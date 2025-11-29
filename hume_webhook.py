import os
import random
import json
import time
from fastapi import FastAPI, HTTPException, Request
from starlette.responses import JSONResponse
from hume.client import AsyncHumeClient
from hume.empathic_voice.control_plane.client import AsyncControlPlaneClient
from hume.empathic_voice.types import (
    WebhookEvent,
    WebhookEventChatStarted,
    WebhookEventChatEnded,
    WebhookEventToolCall
)
from hume.empathic_voice import ToolCallMessage, ToolErrorMessage, ToolResponseMessage
import uvicorn
import httpx

# FastAPI app instance
app = FastAPI()

# API Key - get from environment or use fallback
HUME_API_KEY = os.getenv("HUME_API_KEY", "ZvEVO2dQuKoVcshTyW6zVs48aVir5FJgpMnTyKGvZkt7FzYg")

# Syncronizer.io credentials
SYNCRONIZER_API_KEY = "dXNlci0xMTc3LXNhbmRib3g.rHgHePj9Lfz7DhEGKL7CMuvA2HRRx7Wo"
SYNCRONIZER_SUBDOMAIN = "sabastian-demo-practice"
SYNCRONIZER_LOCATION_ID = 334724
SYNCRONIZER_BASE_URL = "https://nexhealth.info"

# Bearer token cache (will be fetched from authentication)
_bearer_token = None
_token_expires_at = None

# Instantiate the Hume clients
client = AsyncHumeClient(api_key=HUME_API_KEY)
control_plane_client = AsyncControlPlaneClient(client_wrapper=client._client_wrapper)

# Dad joke generator
def get_dad_joke():
    """Generate a random dad joke."""
    jokes = [
        "Why don't scientists trust atoms? Because they make up everything.",
        "I only know 25 letters of the alphabet. I don't know y.",
        "Why did the scarecrow win an award? Because he was outstanding in his field.",
        "Why don't eggs tell jokes? They'd crack each other up.",
        "What do you call fake spaghetti? An impasta.",
        "I used to hate facial hair, but then it grew on me.",
        "What do you call a bear with no teeth? A gummy bear!",
        "Why don't scientists trust atoms? Because they make up everything!",
        "What's the best thing about Switzerland? I don't know, but the flag is a big plus.",
        "Why did the math book look so sad? Because it was full of problems."
    ]
    return random.choice(jokes)

# Syncronizer.io API functions
async def authenticate_syncronizer():
    """
    Authenticate with Syncronizer.io API to get bearer token.
    
    Returns:
        Bearer token string or None if authentication fails
    """
    global _bearer_token, _token_expires_at
    
    try:
        headers = {
            "Accept": "application/vnd.Nexhealth+json;version=2",
            "Authorization": SYNCRONIZER_API_KEY  # API key for authentication
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SYNCRONIZER_BASE_URL}/authenticates",
                headers=headers,
                timeout=10.0
            )
            
            if response.status_code in [200, 201]:  # Accept both 200 OK and 201 Created
                data = response.json()
                if data.get("code") and "data" in data and "token" in data["data"]:
                    _bearer_token = data["data"]["token"]
                    # Tokens typically expire in 1 hour, set expiry to 50 minutes for safety
                    _token_expires_at = time.time() + 3000  # 50 minutes
                    print(f"[AUTH] Successfully authenticated with Syncronizer.io")
                    return _bearer_token
                else:
                    print(f"[AUTH ERROR] Unexpected response format: {data}")
                    return None
            else:
                print(f"[AUTH ERROR] Authentication failed: {response.status_code} - {response.text}")
                return None
                
    except Exception as e:
        print(f"[AUTH ERROR] Authentication exception: {str(e)}")
        return None

async def get_bearer_token():
    """
    Get valid bearer token, refreshing if necessary.
    
    Returns:
        Valid bearer token or None if authentication fails
    """
    global _bearer_token, _token_expires_at
    
    current_time = time.time()
    
    # Check if we have a valid token
    if _bearer_token and _token_expires_at and current_time < _token_expires_at:
        return _bearer_token
    
    # Token is expired or doesn't exist, authenticate
    print("[AUTH] Bearer token expired or missing, authenticating...")
    return await authenticate_syncronizer()

async def search_patients(name=None, phone_number=None, email=None, date_of_birth=None):
    """
    Search for patients using the Syncronizer.io API.
    
    Args:
        name: Patient name (optional)
        phone_number: Patient phone number (optional)
        email: Patient email (optional)
        date_of_birth: Patient DOB in YYYY-MM-DD format (optional)
    
    Returns:
        List of matching patients or error message
    """
    try:
        # Get valid bearer token
        bearer_token = await get_bearer_token()
        if not bearer_token:
            return {
                "success": False,
                "message": "Authentication failed. Unable to access patient records.",
                "patients": []
            }
        
        # Prepare query parameters
        params = {
            "subdomain": SYNCRONIZER_SUBDOMAIN,
            "location_id": SYNCRONIZER_LOCATION_ID,
            "per_page": 10  # Limit results for voice agent
        }
        
        # Add search filters if provided
        if name:
            params["name"] = name
        if phone_number:
            # Clean phone number (remove spaces, dashes, parentheses)
            clean_phone = ''.join(filter(str.isdigit, phone_number))
            params["phone_number"] = clean_phone
        if email:
            params["email"] = email
        if date_of_birth:
            params["date_of_birth"] = date_of_birth
        
        # Set up headers with bearer token
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {bearer_token}",
            "Nex-Api-Version": "v20240412"
        }
        
        # Make API request
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SYNCRONIZER_BASE_URL}/patients",
                params=params,
                headers=headers,
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                patients = data.get("data", [])
                
                if not patients:
                    return {
                        "success": False,
                        "message": "No patients found matching your search criteria.",
                        "patients": []
                    }
                
                # Format patient results for voice agent
                formatted_patients = []
                for patient in patients[:5]:  # Limit to 5 results for voice
                    formatted_patient = {
                        "id": patient.get("id"),
                        "name": f"{patient.get('first_name', '')} {patient.get('last_name', '')}".strip(),
                        "phone": patient.get("phone_number"),
                        "email": patient.get("email"),
                        "date_of_birth": patient.get("date_of_birth")
                    }
                    formatted_patients.append(formatted_patient)
                
                return {
                    "success": True,
                    "message": f"Found {len(patients)} patient(s) matching your search.",
                    "patients": formatted_patients,
                    "total_count": data.get("count", len(patients))
                }
            
            else:
                return {
                    "success": False,
                    "message": f"API error: {response.status_code} - {response.text}",
                    "patients": []
                }
                
    except httpx.TimeoutException:
        return {
            "success": False,
            "message": "Request timed out. Please try again.",
            "patients": []
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Error searching patients: {str(e)}",
            "patients": []
        }

async def get_providers(location_id=None, requestable=None, provider_name=None):
    """
    Get providers (doctors, dentists, hygienists) from the Syncronizer.io API.
    
    Args:
        location_id: Filter by specific location (optional)
        requestable: Only providers accepting online scheduling (optional)
        provider_name: Provider name to search for (optional, for filtering results)
    
    Returns:
        List of providers or error message
    """
    try:
        # Get valid bearer token
        bearer_token = await get_bearer_token()
        if not bearer_token:
            return {
                "success": False,
                "message": "Authentication failed. Unable to access provider information.",
                "providers": []
            }
        
        # Prepare query parameters
        params = {
            "subdomain": SYNCRONIZER_SUBDOMAIN,
            "per_page": 20  # Reasonable limit for voice agent
        }
        
        # Add optional filters
        if location_id:
            params["location_id"] = location_id
        if requestable is not None:
            params["requestable"] = requestable
        
        # Set up headers with bearer token
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {bearer_token}",
            "Nex-Api-Version": "v20240412"
        }
        
        # Make API request
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SYNCRONIZER_BASE_URL}/providers",
                params=params,
                headers=headers,
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                providers = data.get("data", [])
                
                # Filter by provider name if specified (client-side filtering)
                if provider_name:
                    filtered_providers = []
                    search_name = provider_name.lower()
                    for provider in providers:
                        provider_full_name = f"{provider.get('first_name', '')} {provider.get('last_name', '')}".strip().lower()
                        provider_last_name = provider.get('last_name', '').lower()
                        
                        if (search_name in provider_full_name or 
                            search_name in provider_last_name or
                            provider_last_name.startswith(search_name)):
                            filtered_providers.append(provider)
                    providers = filtered_providers
                
                if not providers:
                    return {
                        "success": False,
                        "message": "No providers found matching your criteria.",
                        "providers": []
                    }
                
                # Format provider results for voice agent
                formatted_providers = []
                for provider in providers[:10]:  # Limit to 10 for voice
                    formatted_provider = {
                        "id": provider.get("id"),
                        "name": f"Dr. {provider.get('first_name', '')} {provider.get('last_name', '')}".strip(),
                        "first_name": provider.get("first_name"),
                        "last_name": provider.get("last_name"),
                        "title": provider.get("title", "Doctor"),
                        "speciality": provider.get("speciality"),
                        "requestable": provider.get("requestable", True)
                    }
                    formatted_providers.append(formatted_provider)
                
                return {
                    "success": True,
                    "message": f"Found {len(providers)} provider(s).",
                    "providers": formatted_providers,
                    "total_count": data.get("count", len(providers))
                }
            
            else:
                return {
                    "success": False,
                    "message": f"API error: {response.status_code} - {response.text}",
                    "providers": []
                }
                
    except httpx.TimeoutException:
        return {
            "success": False,
            "message": "Request timed out. Please try again.",
            "providers": []
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Error retrieving providers: {str(e)}",
            "providers": []
        }

async def handle_search_patients_tool(control_plane_client: AsyncControlPlaneClient, chat_id: str, tool_call_message: ToolCallMessage):
    """
    Handle the search_patients tool call and send the response back to the chat.
    
    Args:
        control_plane_client: The control plane client instance
        chat_id: The ID of the chat
        tool_call_message: The tool call message
    """
    tool_call_id = tool_call_message.tool_call_id
    tool_name = tool_call_message.name
    
    print(f"[TOOL] Processing tool: {tool_name}")
    print(f"[TOOL] Tool call ID: {tool_call_id}")
    
    if tool_name != "search_patients":
        print(f"[ERROR] Unknown tool: {tool_name}")
        return
    
    try:
        # Parse tool parameters (they come as JSON string)
        parameters_str = tool_call_message.parameters or "{}"
        
        # Parse JSON string to dictionary
        if isinstance(parameters_str, str):
            parameters = json.loads(parameters_str)
        else:
            parameters = parameters_str or {}
        
        # Extract search parameters
        name = parameters.get("name")
        phone_number = parameters.get("phone_number") 
        email = parameters.get("email")
        date_of_birth = parameters.get("date_of_birth")
        
        print(f"[SEARCH] Searching patients with: name={name}, phone={phone_number}, email={email}, dob={date_of_birth}")
        
        # Search for patients
        result = await search_patients(
            name=name,
            phone_number=phone_number,
            email=email,
            date_of_birth=date_of_birth
        )
        
        # Format response for voice agent
        if result["success"]:
            if result["patients"]:
                # Format patient list for natural speech
                patient_list = []
                for patient in result["patients"]:
                    patient_info = f"{patient['name']}"
                    if patient.get('phone'):
                        patient_info += f" (phone: {patient['phone']})"
                    if patient.get('date_of_birth'):
                        patient_info += f" (DOB: {patient['date_of_birth']})"
                    patient_list.append(patient_info)
                
                if len(patient_list) == 1:
                    response_content = f"I found 1 patient: {patient_list[0]}. Is this the correct patient?"
                else:
                    response_content = f"I found {len(patient_list)} patients:\n"
                    for i, patient in enumerate(patient_list, 1):
                        response_content += f"{i}. {patient}\n"
                    response_content += "Which patient would you like to select?"
            else:
                response_content = "I couldn't find any patients matching your search. Could you please verify the spelling of the name, or try providing a phone number or date of birth?"
        else:
            response_content = f"I encountered an issue while searching for patients: {result['message']}"
        
        # Send the result as a tool response
        await control_plane_client.send(
            chat_id=chat_id,
            request=ToolResponseMessage(
                tool_call_id=tool_call_id,
                content=response_content
            )
        )
        print(f"[SUCCESS] Patient search completed successfully!")
        
    except Exception as e:
        print(f"[ERROR] Failed to handle search patients tool: {e}")
        
        # Send error response
        await control_plane_client.send(
            chat_id=chat_id,
            request=ToolErrorMessage(
                tool_call_id=tool_call_id,
                error="PatientSearchError",
                content=f"I'm having trouble searching for patients right now. Please try again or contact our office directly. Error: {str(e)}"
            )
            )

async def handle_get_providers_tool(control_plane_client: AsyncControlPlaneClient, chat_id: str, tool_call_message: ToolCallMessage):
    """
    Handle the get_providers tool call and send the response back to the chat.
    
    Args:
        control_plane_client: The control plane client instance
        chat_id: The ID of the chat
        tool_call_message: The tool call message
    """
    tool_call_id = tool_call_message.tool_call_id
    tool_name = tool_call_message.name
    
    print(f"[TOOL] Processing tool: {tool_name}")
    print(f"[TOOL] Tool call ID: {tool_call_id}")
    
    if tool_name != "get_providers":
        print(f"[ERROR] Unknown tool: {tool_name}")
        return
    
    try:
        # Parse tool parameters (they come as JSON string)
        parameters_str = tool_call_message.parameters or "{}"
        
        # Parse JSON string to dictionary
        if isinstance(parameters_str, str):
            parameters = json.loads(parameters_str)
        else:
            parameters = parameters_str or {}
        
        # Extract search parameters
        location_id = parameters.get("location_id")
        requestable = parameters.get("requestable") 
        provider_name = parameters.get("provider_name")
        
        print(f"[PROVIDERS] Searching providers with: location_id={location_id}, requestable={requestable}, provider_name={provider_name}")
        
        # Get providers
        result = await get_providers(
            location_id=location_id,
            requestable=requestable,
            provider_name=provider_name
        )
        
        # Format response for voice agent
        if result["success"]:
            if result["providers"]:
                # Format provider list for natural speech
                provider_list = []
                for provider in result["providers"]:
                    provider_info = provider['name']
                    if provider.get('speciality'):
                        provider_info += f" ({provider['speciality']})"
                    if not provider.get('requestable', True):
                        provider_info += " (not available for online booking)"
                    provider_list.append(provider_info)
                
                if len(provider_list) == 1:
                    response_content = f"I found 1 provider: {provider_list[0]}. Would you like to schedule with this doctor?"
                elif len(provider_list) <= 5:
                    response_content = f"I found {len(provider_list)} providers:\n"
                    for i, provider in enumerate(provider_list, 1):
                        response_content += f"{i}. {provider}\n"
                    response_content += "Which doctor would you prefer?"
                else:
                    # Show first 5 if many results
                    response_content = f"I found {len(provider_list)} providers. Here are the first 5:\n"
                    for i, provider in enumerate(provider_list[:5], 1):
                        response_content += f"{i}. {provider}\n"
                    response_content += "Would you like to see more options or choose from these?"
            else:
                if provider_name:
                    response_content = f"I couldn't find a provider named '{provider_name}'. Could you check the spelling or try a different name? I can also show you all available providers."
                else:
                    response_content = "I couldn't find any providers matching your criteria. Let me check our available doctors for you."
        else:
            response_content = f"I encountered an issue while looking up providers: {result['message']}"
        
        # Send the result as a tool response
        await control_plane_client.send(
            chat_id=chat_id,
            request=ToolResponseMessage(
                tool_call_id=tool_call_id,
                content=response_content
            )
        )
        print(f"[SUCCESS] Provider search completed successfully!")
        
    except Exception as e:
        print(f"[ERROR] Failed to handle get providers tool: {e}")
        
        # Send error response
        await control_plane_client.send(
            chat_id=chat_id,
            request=ToolErrorMessage(
                tool_call_id=tool_call_id,
                error="ProviderSearchError",
                content=f"I'm having trouble finding provider information right now. Please try again or contact our office directly. Error: {str(e)}"
            )
        )

async def handle_dad_joke_tool(control_plane_client: AsyncControlPlaneClient, chat_id: str, tool_call_message: ToolCallMessage):
    """
    Handle the tell_dad_joke tool call and send the response back to the chat.
    
    Args:
        control_plane_client: The control plane client instance
        chat_id: The ID of the chat
        tool_call_message: The tool call message
    """
    tool_call_id = tool_call_message.tool_call_id
    tool_name = tool_call_message.name
    
    print(f"[TOOL] Processing tool: {tool_name}")
    print(f"[TOOL] Tool call ID: {tool_call_id}")
    
    if tool_name != "tell_dad_joke":
        print(f"[ERROR] Unknown tool: {tool_name}")
        return
    
    try:
        # Generate a dad joke
        joke = get_dad_joke()
        print(f"[JOKE] Generated joke: {joke}")
        
        # Send the joke as a tool response
        await control_plane_client.send(
            chat_id=chat_id,
            request=ToolResponseMessage(
                tool_call_id=tool_call_id,
                content=joke
            )
        )
        print(f"[SUCCESS] Dad joke sent successfully!")
        
    except Exception as e:
        print(f"[ERROR] Failed to handle dad joke tool: {e}")
        
        # Send error response
        await control_plane_client.send(
            chat_id=chat_id,
            request=ToolErrorMessage(
                tool_call_id=tool_call_id,
                error="DadJokeError",
                content=f"Sorry, I couldn't generate a dad joke right now: {str(e)}"
            )
        )

@app.get("/health")
async def health():
    """Health check endpoint."""
    return JSONResponse({
        "status": "ok", 
        "service": "Hume EVI Dad Joke Webhook",
        "webhook_url": "https://pitchy-incomprehendingly-dianne.ngrok-free.dev/hume-webhook"
    })

@app.post("/hume-webhook")
async def hume_webhook_handler(request: Request, event: WebhookEvent):
    """
    Handle incoming webhook events from Hume's Empathic Voice Interface (EVI).
    
    Processes chat_started, chat_ended, and tool_call events.
    """
    print(f"[WEBHOOK] Received event type: {type(event).__name__}")
    
    if isinstance(event, WebhookEventChatStarted):
        print(f"[CHAT] Chat started: {event.chat_id}")
        print(f"[CHAT] Event data: {event.dict()}")
        
    elif isinstance(event, WebhookEventChatEnded):
        print(f"[CHAT] Chat ended: {event.chat_id}")
        print(f"[CHAT] Event data: {event.dict()}")
        
    elif isinstance(event, WebhookEventToolCall):
        print(f"[TOOL] Tool call received: {event.dict()}")
        
        # Route to appropriate tool handler based on tool name
        tool_name = event.tool_call_message.name
        
        if tool_name == "tell_dad_joke":
            await handle_dad_joke_tool(control_plane_client, event.chat_id, event.tool_call_message)
        elif tool_name == "search_patients":
            await handle_search_patients_tool(control_plane_client, event.chat_id, event.tool_call_message)
        elif tool_name == "get_providers":
            await handle_get_providers_tool(control_plane_client, event.chat_id, event.tool_call_message)
        else:
            print(f"[ERROR] Unknown tool: {tool_name}")
            # Send error response for unknown tools
            await control_plane_client.send(
                chat_id=event.chat_id,
                request=ToolErrorMessage(
                    tool_call_id=event.tool_call_message.tool_call_id,
                    error="UnknownTool",
                    content=f"I don't know how to use the {tool_name} tool. Please contact support."
                )
            )
        
    return JSONResponse({"status": "ok"})

if __name__ == "__main__":
    # Get port from environment (for deployment platforms) or use 5000 for local
    port = int(os.getenv("PORT", 5000))
    host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    
    print("[INFO] Starting Hume EVI Dad Joke Webhook Server")
    print(f"[INFO] Webhook endpoint: http://{host}:{port}/hume-webhook")
    print(f"[INFO] Health check: http://{host}:{port}/health")
    print(f"[INFO] Using API key: {HUME_API_KEY[:10]}...")
    
    uvicorn.run("hume_webhook:app", host=host, port=port, reload=True)
