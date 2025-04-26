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

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("azure-terminal-copilot")

class MCPClient:
    def __init__(self):
        self.session: Optional[ClientSession] = None
        self.exit_stack = AsyncExitStack()
        self.available_tools = []
        self.available_prompts = []
        
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
            sse_client(server_url, headers=headers))
    
        self.session = await self.exit_stack.enter_async_context(
            ClientSession(
                read_stream=streams[0],
                write_stream=streams[1]
            )
        )
        await self.session.initialize()
        
        try:
            response = await self.session.list_tools()
            self.available_tools = response.tools

            tool_names = [tool.name for tool in self.available_tools]

            logger.info(f"Connected to Azure MCP Server with {len(tool_names)} tools")
            print(f"Connected to Azure MCP Server: {server_url}")
            
            if "azmcp-extension-az" not in tool_names:
                logger.warning("Warning: 'azmcp-extension-az' tool not found in available tools")
                print("Warning: 'azmcp-extension-az' tool not found. Azure CLI commands may not work.")
        except Exception as e:
            logger.error(f"Failed to list tools: {str(e)}")
            print(f"Connected but couldn't retrieve tool list: {str(e)}")

    async def send_command(self, command: str) -> Union[Dict[str, Any], List[Any], str]:
        if not self.session:
            raise RuntimeError("Not connected to MCP server. Call connect_to_server first.")
                
        logger.info(f"Processing command: {command}")
        print(f"Processing: {command}")
        
        try:
            azure_command = await self.translate_to_azmcp_command(command)
            if azure_command != command:
                print(f"Translated to: {azure_command}")
            
            response = await self.session.call_tool(
                name="azmcp-extension-az",
                arguments={
                    "command": azure_command
                }
            )
            
            if hasattr(response, 'result'):
                return response.result
                
            if hasattr(response, 'content') and response.content:
                print(f"DEBUG: Response has {len(response.content)} content items")
                
                for i, content_item in enumerate(response.content):
                    if hasattr(content_item, 'text') and content_item.text:
                        print(f"DEBUG: Content item {i} has text: {content_item.text[:50]}...")
                        
                        if content_item.text == "null":
                            return []
                            
                        try:
                            return json.loads(content_item.text)
                        except json.JSONDecodeError:
                            return content_item.text
                
            return {
                "message": "Azure command completed but didn't return usable content."
            }
                
        except Exception as e:
            logger.error(f"Failed to execute command: {str(e)}")
            return {"error": f"Command execution failed: {str(e)}"}
            
    async def translate_to_azmcp_command(self, natural_language_query: str) -> str:
        ollama_host = os.getenv('OLLAMA_HOST')
        ollama_model = os.getenv('OLLAMA_MODEL')
        
        available_commands = []
    
        if self.available_tools:
            available_commands.extend([
            tool.name.replace('azmcp-', '').replace('-', ' ') 
            for tool in self.available_tools 
            if hasattr(tool, 'name')
            ])
        
        available_commands = list(set(available_commands))
        command_list = "\n".join([f"- {cmd}" for cmd in available_commands])
        print(command_list)
        
        system_prompt = f"""
        You are an Azure CLI expert. Translate the user's natural language query into the appropriate
        Azure CLI command based on the available commands.
        
        Available commands:
        {command_list}
        
        Instructions:
        1. Select the most appropriate command from the list above
        2. If the command needs parameters, add them based on the user's query
        3. Respond with ONLY the command, no explanations or additional text
    
        
        If you're not sure, respond with the closest matching command from the available list.
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
                            {"role": "user", "content": natural_language_query}
                        ],
                        "stream": False
                    }
                )
                
                if response.status_code == 200:
                    result = response.json()
                    azure_command = result["message"]["content"].strip()
                    logger.info(f"Translated '{natural_language_query}' to '{azure_command}'")
                    return azure_command
                else:
                    logger.error(f"Ollama API error: {response.status_code} - {response.text}")
                    return natural_language_query
                    
        except Exception as e:
            logger.error(f"Failed to translate query: {str(e)}")
            return natural_language_query

async def main():
    load_dotenv()
    server_url = os.getenv('SERVER_URL')
    ollama_host = os.getenv('OLLAMA_HOST')
    ollama_model = os.getenv('MODEL')
    
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
            print("\n⚠️ Ollama is not available. Natural language queries will be sent directly to Azure.")
         
        
        while True:
            print("\n" + "="*50)
            if ollama_available:
                print("Enter your Azure request in natural language (or 'exit' to quit)")
                print("Example: 'list my resource groups' or 'show my storage accounts'")
            else:
                print("Enter Azure CLI command (or 'exit' to quit)")
                print("Example: 'group list' or 'storage account list'")
            
            user_input = input("> ")
            
            if user_input.lower() in ('exit', 'quit', 'q'):
                break
                
            if not user_input.strip():
                continue
                
            result = await client.send_command(user_input)
            
            logger.info(f"Raw result: {result}")
            
            print("\n🔹 Response:")
            if result is None:
                print("No response received.")
            elif result == []:
                print("No resources found matching your criteria.")
            else:
                print(json.dumps(result, indent=2))
            
    finally:
        await client.cleanup()
        
if __name__ == "__main__":
    asyncio.run(main())