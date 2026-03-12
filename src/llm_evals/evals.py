import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import dotenv
from langchain_community.docstore.document import Document
from langchain_core.globals import set_debug
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage, trim_messages
from langchain_core.prompts import MessagesPlaceholder, ChatPromptTemplate
from langchain_core.runnables import RunnableConfig, RunnablePassthrough
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_qdrant import QdrantVectorStore
from langchain_redis import RedisChatMessageHistory
from langchain_redis.chat_message_history import BaseChatMessageHistory
from langfuse import observe, get_client
from langfuse.langchain import CallbackHandler
from nemoguardrails import RailsConfig
from nemoguardrails.integrations.langchain.runnable_rails import RunnableRails
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams
from redis import Redis
from ulid import ULID

# Load environment variables from .env file
_ = dotenv.load_dotenv()

set_debug(False)

session_name = f"session-{uuid.uuid4().hex[:8]}"
user_id = f"user-{uuid.uuid4().hex[:8]}"

#Initialize redis client
_redis_client: Redis = Redis.from_url(os.getenv("REDIS_CONNECTION_STRING"))

# Initialize the LLM with OpenAI API credentials (substitute for other models)
llm = ChatOpenAI(
    model=os.getenv("OPENAI_MODEL"),
    api_key=os.getenv("OPENAI_API_KEY")
)

# Initialize the embeddings model with OpenAI API credentials
embeddings_model = OpenAIEmbeddings(
    model="text-embedding-ada-002",
    api_key=os.getenv("OPENAI_API_KEY"),
    show_progress_bar=True,
)

# Initialize conversation history
conversation = []

#Initialize Callbackhandler
langfuse_handler = CallbackHandler()

#Initialize Nemo config
# Get the directory of the current file
BASE_DIR = Path(__file__).resolve().parent
config_path: Path = BASE_DIR / ".." / "config"
rails_config = RailsConfig.from_path(str(config_path))


# ---------------------------
# Load JSON Data and Build Qdrant Vector Store
# ---------------------------
@observe(name="embed-documents")
def embed_documents(json_path: str):
    """
    Load JSON data from the smartphones.json file and convert each entry to a Document.
    :param
        json_path (str): Path to the JSON file containing smartphone data.

    :returns
        Qdrant vector store A Qdrant vector store built from the smartphone documents,
                or an empty list if an error occurs.
    """
    langfuse_client = get_client()
    langfuse_client.update_current_trace(user_id=user_id, session_id=session_name)

    try:
        path = Path(json_path)
        with open(path, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Error: The file {json_path} was not found.")
        return []
    except json.JSONDecodeError as jde:
        print(f"Error decoding JSON from file {json_path}: {jde}")
        return []
    except Exception as e:
        print(f"An unexpected error occurred while reading {json_path}: {e}")
        return []

    documents = []
    for entry in data:
        # Build a readable content string from the JSON entry
        content = (
            f"Model: {entry.get('model', '')}\n"
            f"Price: {entry.get('price', '')}\n"
            f"Rating: {entry.get('rating', '')}\n"
            f"SIM: {entry.get('sim', '')}\n"
            f"Processor: {entry.get('processor', '')}\n"
            f"RAM: {entry.get('ram', '')}\n"
            f"Battery: {entry.get('battery', '')}\n"
            f"Display: {entry.get('display', '')}\n"
            f"Camera: {entry.get('camera', '')}\n"
            f"Card: {entry.get('card', '')}\n"
            f"OS: {entry.get('os', '')}\n"
            f"In Stock: {entry.get('in_stock', '')}"
        )
        documents.append(Document(page_content=content))

    try:
        collection_name = "smartphones"
        qdrant_client = QdrantClient("http://localhost:6333")

        collection_exists = qdrant_client.collection_exists(collection_name=collection_name)
        if not collection_exists:
            qdrant_client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(
                    size=1536,
                    distance=Distance.COSINE,
                ),
            )

            qdrant_store = QdrantVectorStore(
                client=qdrant_client,
                collection_name=collection_name,
                embedding=embeddings_model
            )

            qdrant_store.add_documents(documents=documents)

            return qdrant_store

        # no need to create a vector store every time
        else:
            qdrant_store = QdrantVectorStore.from_existing_collection(
                embedding=embeddings_model,
                collection_name=collection_name,
            )
            return qdrant_store

    except Exception as e:
        print(f"Error initializing the vector store: {e}")
        return []


# ---------------------------
# Tool Definitions
# ---------------------------
@tool("SmartphoneInfo")
def smartphone_info_tool(model: str, tool_call_id: str) -> ToolMessage:
    """
    Retrieves information about a smartphone model and returns a ToolMessage.
    """
    product_db = embed_documents("datasets/smartphones.json")

    if isinstance(product_db, list):
        content = f"Error: Vector store could not be initialized."
        return ToolMessage(content=content, tool_call_id=tool_call_id)

    try:
        results = product_db.similarity_search(model, k=1)
        if not results:
            content = "Could not find information for the specified model."
        else:
            content = results[0].page_content

        return ToolMessage(content=content, tool_call_id=tool_call_id)

    except Exception as e:
        return ToolMessage(
            content=f"Error during retrieval: {e}",
            tool_call_id=tool_call_id
        )


# ---------------------------
# Tool Call Handling and Response Generation
# ---------------------------
@observe(name="generate-context")
def generate_context(ai_message: AIMessage) -> list[BaseMessage]:
    """
    Process tool calls from the language model and collect their responses as ToolMessage objects.

    :param
        ai_message (AIMessage): The language model's output message containing tool_calls.

    :returns
        A dictionary containing a list of ToolMessage objects under the key "tool_responses".
    """
    # construct the conversation history with the AI message containing tool calls
    langfuse_client = get_client()
    langfuse_client.update_current_trace(user_id=user_id, session_id=session_name)
    current_conversation: list[BaseMessage] = []

    # Check if the AI message has any tool calls
    if not hasattr(ai_message, "tool_calls") or not ai_message.tool_calls:
        current_conversation.append(ai_message)
        return current_conversation
    else:
        current_conversation.append(ai_message)

    try:
        # Process each tool call, invoke the appropriate tool, and append the result to the conversation
        # a message with tool calls is expected to be followed by tool responses
        for tool_call in ai_message.tool_calls:
            if tool_call["name"] == "SmartphoneInfo":
                # LangChain tool's .invoke(tool_call) passes the 'args' dict to the function
                # We need to manually pass tool_call_id if it's a separate parameter
                tool_output: ToolMessage = smartphone_info_tool.invoke({
                    "model": tool_call["args"]["model"],
                    "tool_call_id": tool_call["id"]
                })
                current_conversation.append(tool_output)

    except Exception as e:
        print(f"An error occurred while processing tool calls: {e}")
        for tool_call in ai_message.tool_calls:
        # Check if this tool_call was already answered successfully before the crash
            if not any(isinstance(m, ToolMessage) and m.tool_call_id == tool_call["id"] for m in current_conversation):
                current_conversation.append(
                    ToolMessage(
                        content=f"Error processing tool: {e}",
                        tool_call_id=tool_call["id"]
                    )
                )
    finally:
        return current_conversation



def get_redis_history(session_id: str) -> BaseChatMessageHistory:
    return ImprovedRedisChatMessageHistory(
        session_id=session_id,
        redis_client=_redis_client,
        ttl=os.getenv("REDIS_TTL_S")
    )


class ImprovedRedisChatMessageHistory(RedisChatMessageHistory):
    def add_message(self, message: BaseMessage) -> None:
        if message is None:
            raise ValueError("Message cannot be None")

        timestamp = datetime.now().timestamp()
        message_id = str(ULID())

        data = {
            "content": message.content,
            "additional_kwargs": message.additional_kwargs,
            "type": message.type,
        }

        if isinstance(message, AIMessage):
            if message.tool_calls:
                data["tool_calls"] = message.tool_calls
            if message.invalid_tool_calls:
                data["invalid_tool_calls"] = message.invalid_tool_calls
            if message.usage_metadata:
                data["usage_metadata"] = message.usage_metadata

        common_data_to_store: Dict[str, Any] = {
            "type": message.type,
            "message_id": message_id,
            "data": data,
            "session_id": self.session_id,
            "timestamp": timestamp,
        }

        if isinstance(message, ToolMessage):
            common_data_to_store["data"]["tool_call_id"] = message.tool_call_id
            common_data_to_store["data"]["status"] = message.status

        self.index.load(
            data=[common_data_to_store],
            keys=[self._message_key(message_id)],
            ttl=self.ttl,
        )


# ---------------------------
# Main Conversation Loop
# ---------------------------
@observe(name="main-loop")
def main() -> None:
    langfuse_client = get_client()
    langfuse_client.update_current_trace(user_id=user_id, session_id=session_name)

    # List of available tools
    tools = [smartphone_info_tool]

    # Bind the tools to the language model instance
    llm_with_tools = llm.bind_tools(tools)


    context_system_prompt = langfuse_client.get_prompt("context_system_prompt", label="latest")
    review_system_prompt = langfuse_client.get_prompt("review_system_prompt", label="latest")
    goodbye_system_prompt = langfuse_client.get_prompt("goodbye_system_prompt", label="latest")

    rails = RunnableRails(rails_config, input_key="user_input")

    context_prompt = ChatPromptTemplate.from_messages(
        [
            context_system_prompt.get_langchain_prompt()[0],
            MessagesPlaceholder(variable_name="conversation"),
            context_system_prompt.get_langchain_prompt()[1]

        ]
    )

    context_prompt.metadata = {"langfuse_prompt": context_system_prompt}

    review_prompt = ChatPromptTemplate.from_messages(
        [
            review_system_prompt.get_langchain_prompt()[0],
            review_system_prompt.get_langchain_prompt()[1],
            MessagesPlaceholder(variable_name="conversation")
        ]
    )

    review_prompt.metadata = {"langfuse_prompt": review_system_prompt}

    goodbye_prompt = ChatPromptTemplate.from_messages(
            goodbye_system_prompt.get_langchain_prompt()[0]
    )

    goodbye_prompt.metadata = {"langfuse_prompt": goodbye_system_prompt}

    trimmer = trim_messages(
        strategy="last",  # keep either the last or first messages
        token_counter=llm,  # use your LLM to count tokens or create a special function
        max_tokens=500,  # the maximum number of tokens
        start_on="human",  # the first message type in the trimmed history
        end_on=("human", "tool"),
        include_system=True,  # always include the system message
    )



    context_chain = context_prompt | trimmer | llm_with_tools | generate_context

    context_chain_with_history = RunnableWithMessageHistory(
        runnable=context_chain,
        get_session_history=get_redis_history,
        input_messages_key="user_input",
        history_messages_key="conversation"
    )

    context_chain_with_rails = (RunnablePassthrough.assign(output=rails) | context_chain_with_history)

    review_chain = review_prompt | trimmer | llm

    review_chain_with_history = RunnableWithMessageHistory(
        runnable=review_chain,
        get_session_history=get_redis_history,
        input_messages_key="user_input",
        history_messages_key="conversation"
    )

    review_chain_with_rails = (RunnablePassthrough.assign(output=rails) | review_chain_with_history)

    goodbye_chain = goodbye_prompt | llm

    try:
        print("Welcome to the Smartphone Assistant! I can help you with smartphone features and comparisons.")
        while True:
            user_input = input("User: ").strip()
            if user_input.lower() in ["exit", "quit", "bye", "end"]:
                goodbye_message = goodbye_chain.invoke(
                   input={"user_id": user_id},
                   config=RunnableConfig(
                       run_name="goodbye-message",
                       callbacks=[langfuse_handler],
                       metadata={
                           "langfuse_session_id": session_name,
                           "langfuse_user_id": user_id,
                       }
                   ))
                current_trace = langfuse_client.get_current_trace_id()
                print("-*" * 20)
                user_score: int = 0
                try:
                    user_score = int(input(f"> How would you rate the relevance of my responses for trace_id {current_trace} (0-10)?: ").strip())
                except Exception as e:
                    print("Unable to capture score")

                user_comments: str = ""
                try:
                    user_comments = input("> Would you like to add any comments?: ").strip()
                except Exception as e:
                    print("Unable to capture comments")
                print("-*" * 20)


                langfuse_client.score_current_trace(
                    name="relevance",
                    value=user_score/10,
                    data_type="NUMERIC",
                    comment=user_comments
                )

                print(f"System: {goodbye_message.content}")
                break

            context_chain_with_rails.invoke({"user_input": user_input},
                                config=RunnableConfig(
                                       configurable={"session_id": session_name},
                                       run_name="context",
                                       callbacks=[langfuse_handler],
                                       metadata={
                                           "langfuse_session_id": session_name,
                                           "langfuse_user_id": user_id,
                                       }
                                   ))

            response = review_chain_with_rails.invoke({"user_id": user_id, "user_input": user_input},
                               config=RunnableConfig(
                                   configurable={"session_id": session_name},
                                   run_name="final-response",
                                   callbacks=[langfuse_handler],
                                   metadata={
                                       "langfuse_session_id": session_name,
                                       "langfuse_user_id": user_id,
                                   }
                               ))

            print(f"System: {response.content}")

    except Exception as e:
        print(f"An unexpected error occurred in the main loop: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

