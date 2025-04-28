import asyncio
import json
import os
import logging
from mcp.client.sse import sse_client
from mcp import ClientSession
from dotenv import load_dotenv
from contextlib import AsyncExitStack
from typing import Any, Dict, Optional, List, Union
import httpx
from rich.console import Console
from rich.table import Table

logging.basicConfig(
    level=logging.WARNING, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("azure-terminal-copilot")


class MCPClient:
    def __init__(self):
        self.session: Optional[ClientSession] = None
        self.exit_stack = AsyncExitStack()
        self.available_tools = []
        self.console = Console()

    async def cleanup(self):
        if self.exit_stack:
            await self.exit_stack.aclose()
            logger.info("Resources cleaned up")

    async def connect_to_server(self, server_url: str = None, api_key: str = None):
        if not server_url:
            raise ValueError("server_url is required")

        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        logger.info(f"Connecting to Azure MCP Server: {server_url}")
        streams = await self.exit_stack.enter_async_context(
            sse_client(server_url, headers=headers)
        )

        self.session = await self.exit_stack.enter_async_context(
            ClientSession(read_stream=streams[0], write_stream=streams[1])
        )
        await self.session.initialize()

        try:
            response = await self.session.list_tools()
            self.available_tools = response.tools

            tool_names = [tool.name for tool in self.available_tools]

            logger.info(f"Connected to Azure MCP Server with {len(tool_names)} tools")
            print(f"Connected to Azure MCP Server: {server_url}")

            if "azmcp-extension-az" not in tool_names:
                logger.warning(
                    "Warning: 'azmcp-extension-az' tool not found in available tools"
                )
                print(
                    "Warning: 'azmcp-extension-az' tool not found. Azure CLI commands may not work."
                )
        except Exception as e:
            logger.error(f"Failed to list tools: {str(e)}")
            print(f"Connected but couldn't retrieve tool list: {str(e)}")

    async def send_command(
        self, command: str, ollama_host: str, ollama_model: str
    ) -> Union[Dict[str, Any], List[Any], str]:
        if not self.session:
            raise RuntimeError(
                "Not connected to MCP server. Call connect_to_server first."
            )
    
        logger.info(f"Processing command: {command}")
        print(f"Processing: {command}")
    
        try:
            table_requested = any(term in command.lower() for term in 
                                 ['show as table', 'as a table', 'in table format', 'display table',
                                  'format as table', 'table view', 'show table', 'in table view'])
            
            azure_command = await self.translate_to_azmcp_command(
                command, ollama_host, ollama_model
            )
            if azure_command != command:
                print(f"Translated to: {azure_command}")
            
            result_metadata = {
                "original_command": command,
                "azure_command": azure_command,
                "table_requested": table_requested
            }
            
            response = await self.session.call_tool(
                name="azmcp-extension-az", arguments={"command": azure_command}
            )

            result = None
            if hasattr(response, "result"):
                result = response.result
            elif hasattr(response, "content") and response.content:
                for content_item in response.content:
                    if hasattr(content_item, "text") and content_item.text:
                        if content_item.text == "null":
                            return {"result": [], "metadata": result_metadata}
                        try:
                            result = json.loads(content_item.text)
                        except json.JSONDecodeError:
                            result = content_item.text
                        break
    
            if result is None:
                return {"result": {"message": "Command completed but didn't return usable content."}, 
                        "metadata": result_metadata}
                
            return {"result": result, "metadata": result_metadata}
    
        except Exception as e:
            logger.error(f"Failed to execute command: {str(e)}")
            return {"error": f"Command execution failed: {str(e)}"}

    async def translate_to_azmcp_command(
        self, natural_language_query: str, ollama_host: str, ollama_model: str
    ) -> str:
        available_commands = []

        if self.available_tools:
            available_commands.extend(
                [
                    tool.name.replace("azmcp-", "").replace("-", " ")
                    for tool in self.available_tools
                    if hasattr(tool, "name")
                ]
            )

            available_commands = list(set(available_commands))
            command_list = "\n".join([f"- {cmd}" for cmd in available_commands])

            system_prompt = f"""
                You are an Azure CLI expert. Translate the user's 
                natural language query into the appropriate
                Azure CLI command based on the available commands.
        
                Available commands:
                {command_list}
                
                Only return the suggested command and nothing else.
                
                """

        try:
            logger.info(f"Calling Ollama to translate '{natural_language_query}'")

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{ollama_host}/api/chat",
                    json={
                        "model": ollama_model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": natural_language_query},
                        ],
                        "stream": False,
                    },
                )

                if response.status_code == 200:
                    result = response.json()
                    azure_command = result["message"]["content"].strip()
                    logger.info(
                        f"Translated '{natural_language_query}' to '{azure_command}'"
                    )
                    return azure_command
                else:
                    logger.error(
                        f"Ollama API error: {response.status_code} - {response.text}"
                    )
                    return natural_language_query

        except Exception as e:
            logger.error(f"Failed to translate query: {str(e)}")
            return natural_language_query

    def display_as_table(self, data: Any):
        """Display data as a table using all keys as headers."""
        if not data:
            self.console.print("No data returned from the command.")
            return
    
        if isinstance(data, str):
            self.console.print(data)
            return

        if isinstance(data, dict):
            if "value" in data and isinstance(data["value"], list) and data["value"]:
                data = data["value"]
    
            elif "output" in data:
                if isinstance(data["output"], str):
                    try:
                        data = json.loads(data["output"])
                    except json.JSONDecodeError:
                        self.console.print(data["output"])
                        return
                else:
                    data = data["output"]
                    

        if isinstance(data, list):
            if not data:
                self.console.print("No resources found matching your criteria.")
                return
                
            if not isinstance(data[0], dict):
                self.console.print(json.dumps(data, indent=2))
                return
           
            all_keys = set()
            for item in data:
                if isinstance(item, dict):
                    all_keys.update(item.keys())
                    
  
            important_fields = ["name", "id", "location", "resourceGroup", "type", "status", "provisioningState"]
            headers = [k for k in all_keys if not k.startswith('_')]
            headers.sort(key=lambda x: important_fields.index(x.lower()) if x.lower() in [f.lower() for f in important_fields] else 999)
            
            table = Table(show_header=True, header_style="bold cyan")
            for header in headers[:10]: 
                display_name = header.replace('_', ' ').title()
                table.add_column(display_name)
                
            for item in data:
                row = []
                for header in headers[:10]:
                    value = item.get(header, "")
                    if isinstance(value, (dict, list)):
                        value = str(value)[:50] + "..." if len(str(value)) > 50 else str(value)
                    row.append(str(value) if value is not None else "")
                table.add_row(*row)
                
            self.console.print(table)
            self.console.print(f"[dim]Total: {len(data)} items[/dim]")
            
        elif isinstance(data, dict):
            table = Table(show_header=True, header_style="bold cyan")
            table.add_column("Property")
            table.add_column("Value")
            
            for key, value in {k: v for k, v in data.items() if not k.startswith('_')}.items():
                property_name = key.replace('_', ' ').title()
                formatted_value = str(value)
                if isinstance(value, (dict, list)):
                    formatted_value = str(value)[:80] + "..." if len(str(value)) > 80 else str(value)
                    
                table.add_row(property_name, formatted_value if value is not None else "")
                
            self.console.print(table)
        else:
            self.console.print(data)
    
async def main():
    load_dotenv()
    server_url = os.getenv("SERVER_URL")
    ollama_host = os.getenv("OLLAMA_HOST")
    ollama_model = os.getenv("OLLAMA_MODEL")

    if not server_url:
        print("ERROR: SERVER_URL environment variable not set")
        return

    ollama_available = False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{ollama_host}/api/version")
            if response.status_code == 200:
                ollama_available = True
    except Exception:
        pass

    client = MCPClient()

    try:
        await client.connect_to_server(server_url)

        if ollama_available:
            print("\n✓ Ollama is available for natural language processing")
            print(f"   Using model: {ollama_model}")
        else:
            print(
                "\n⚠️ Ollama is not available. Natural language queries will be sent directly to Azure."
            )

        while True:
            print("\n" + "=" * 50)
            if ollama_available:
                print(
                    "Enter your Azure request in natural language (or 'exit' to quit)"
                )
                print(
                    "Example: 'list my resource groups' or 'show my storage accounts'"
                )
            else:
                print("Enter Azure CLI command (or 'exit' to quit)")
                print("Example: 'group list' or 'storage account list'")

            user_input = input("> ")

            if user_input.lower() in ("exit", "quit", "q"):
                break

            if not user_input.strip():
                continue
            
            result = await client.send_command(user_input, ollama_host, ollama_model)
            logger.info(f"Raw result: {result}")
            
            print("\n🔹 Response:")
            if result is None:
                print("No response received.")
            elif isinstance(result, dict) and "error" in result:
                print(f"Error: {result['error']}")
            elif isinstance(result, dict) and "result" in result:
                actual_result = result["result"]
                metadata = result.get("metadata", {})
                table_requested = metadata.get("table_requested", False)
                
                if table_requested:
                    client.display_as_table(actual_result)
                else:
    
                    if isinstance(actual_result, dict) and "output" in actual_result:
                        output = actual_result["output"]
                        if isinstance(output, str):
                            print(output)
                        else:
                            print(json.dumps(output, indent=2)) 
                    else:
                        print(json.dumps(actual_result, indent=2))
            elif result == []:
                print("No resources found matching your criteria.")
            else:
               
                print(json.dumps(result, indent=2))
    finally:
        await client.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
