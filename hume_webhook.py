import os
import random
import json
import time
from datetime import datetime
from contextvars import ContextVar
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
from hume.core.api_error import ApiError
import uvicorn
import httpx
from supabase import create_client, Client

# FastAPI app instance
app = FastAPI()

# API Key - get from environment
HUME_API_KEY = os.getenv("HUME_API_KEY")

# Syncronizer.io credentials
SYNCRONIZER_API_KEY = os.getenv("SYNCRONIZER_API_KEY")
SYNCRONIZER_SUBDOMAIN = os.getenv("SYNCRONIZER_SUBDOMAIN")
SYNCRONIZER_LOCATION_ID = int(os.getenv("SYNCRONIZER_LOCATION_ID", "0"))
SYNCRONIZER_BASE_URL = os.getenv("SYNCRONIZER_BASE_URL")

# Bearer token cache (will be fetched from authentication)
_bearer_token = None
_token_expires_at = None

# Instantiate the Hume clients
client = AsyncHumeClient(api_key=HUME_API_KEY)
control_plane_client = AsyncControlPlaneClient(client_wrapper=client._client_wrapper)

# Supabase client for event logging
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase_client: Client = None

# Initialize Supabase client if credentials are provided
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("[SUPABASE] Client initialized successfully")
    except Exception as e:
        print(f"[SUPABASE ERROR] Failed to initialize client: {e}")
        supabase_client = None
else:
    print("[SUPABASE WARNING] No credentials found - logging disabled")

# Twilio configuration for outbound calls (set these in environment variables)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

# Call forwarding configuration - number to transfer calls to
CALL_FORWARD_NUMBER = os.getenv("CALL_FORWARD_NUMBER")

# Hume EVI Config IDs (set these in environment variables)
HUME_CONFIG_ID = os.getenv("HUME_CONFIG_ID")  # Inbound calls
HUME_OUTBOUND_CONFIG_ID = os.getenv("HUME_OUTBOUND_CONFIG_ID")  # Outbound reminder calls

# Test mode - bypasses time checks for outbound calls (set to "true" to enable)
OUTBOUND_TEST_MODE = os.getenv("OUTBOUND_TEST_MODE", "false").lower() == "true"

# Vercel URL for callbacks
# IMPORTANT: Use a stable production URL for Twilio callbacks, NOT the preview deployment URL
# The VERCEL_URL env var gives preview URLs like "hume-tool-call-abc123-account.vercel.app" which are temporary
# For Twilio callbacks, we need the stable production URL
TWILIO_CALLBACK_URL = os.getenv("TWILIO_CALLBACK_URL")

# General Vercel URL (may be preview URL during deployments)
_raw_vercel_url = os.getenv("VERCEL_URL", os.getenv("WEBHOOK_URL", ""))
# Ensure URL has https:// prefix (Vercel sometimes provides URL without protocol)
VERCEL_URL = _raw_vercel_url if _raw_vercel_url.startswith('http') else f"https://{_raw_vercel_url}"

# Twilio client for outbound calls
twilio_client = None
TwiML_VoiceResponse = None
try:
    from twilio.rest import Client as TwilioClient
    from twilio.twiml.voice_response import VoiceResponse as TwiML_VoiceResponse
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        print("[TWILIO] Client initialized successfully")
except ImportError:
    print("[TWILIO WARNING] Twilio library not installed - outbound calls disabled")
except Exception as e:
    print(f"[TWILIO ERROR] Failed to initialize client: {e}")

# Context variables for tracking current tool call (for API logging)
_current_chat_id: ContextVar[str] = ContextVar('current_chat_id', default=None)
_current_tool_call_id: ContextVar[str] = ContextVar('current_tool_call_id', default=None)

# Helper function to safely send messages to control plane
async def safe_send_to_control_plane(control_plane_client: AsyncControlPlaneClient, chat_id: str, message):
    """
    Safely send a message to the control plane, handling chat unavailability errors.
    
    Args:
        control_plane_client: The control plane client instance
        chat_id: The ID of the chat
        message: The message to send (ToolResponseMessage or ToolErrorMessage)
    
    Returns:
        bool: True if sent successfully, False if chat is unavailable
    """
    try:
        await control_plane_client.send(
            chat_id=chat_id,
            request=message
        )
        return True
    except ApiError as e:
        # Handle chat unavailability gracefully
        if e.status_code == 400 and 'chat_unavailable' in str(e.body).lower():
            print(f"[WARNING] Chat {chat_id} is no longer available. Skipping response.")
            return False
        else:
            # Re-raise other API errors
            print(f"[ERROR] API Error while sending to control plane: {e}")
            raise
    except Exception as e:
        print(f"[ERROR] Unexpected error sending to control plane: {e}")
        raise

# =====================================================
# SUPABASE LOGGING FUNCTIONS
# =====================================================
"""
EVENT LOGGING SYSTEM

This system logs all Hume EVI interactions and NexHealth API calls to Supabase
for analytics, debugging, and compliance purposes.

WHAT GETS LOGGED:
1. Call Sessions (call_sessions table):
   - chat_started events: When a call begins
   - chat_ended events: When a call ends
   - Includes: chat_id, caller_number, timestamps, full payloads
   
2. Tool Calls (tool_call_events table):
   - Every tool invocation from Hume AI
   - Parameters, execution time, success/failure
   - Response content sent back to Hume
   - Linked to call_sessions via chat_id
   
HOW IT WORKS:
- log_and_execute_tool() wraps all tool handlers
- Context variables (_current_chat_id, _current_tool_call_id) track the current execution context
- logged_httpx_request() can wrap httpx calls to automatically log API calls
- All logging failures are caught and logged but don't crash the webhook

USAGE:
1. Wrap tool handlers with log_and_execute_tool() (already done in webhook router)
2. Use logged_httpx_request() instead of httpx.AsyncClient() for HTTP requests

PRIVACY & SECURITY:
- Authorization headers are redacted in logs
- Patient data is logged for debugging but should be protected by Supabase RLS
- Consider implementing data retention policies
"""

async def log_call_session_start(chat_id: str, chat_group_id: str, config_id: str, caller_number: str, full_payload: dict):
    """
    Log the start of a call session to Supabase.
    
    Args:
        chat_id: Unique chat ID from Hume
        chat_group_id: Chat group ID
        config_id: EVI config ID
        caller_number: Phone number of caller
        full_payload: Complete webhook payload
    """
    if not supabase_client:
        return
    
    try:
        data = {
            "chat_id": chat_id,
            "chat_group_id": chat_group_id,
            "config_id": config_id,
            "caller_number": caller_number,
            "started_at": datetime.utcnow().isoformat(),
            "status": "active",
            "chat_started_payload": full_payload
        }
        
        result = supabase_client.table("call_sessions").insert(data).execute()
        print(f"[SUPABASE] Logged call session start: {chat_id}")
        return result
    except Exception as e:
        print(f"[SUPABASE ERROR] Failed to log call session start: {e}")
        return None

async def log_call_session_end(chat_id: str, full_payload: dict):
    """
    Log the end of a call session to Supabase.
    
    Args:
        chat_id: Unique chat ID from Hume
        full_payload: Complete webhook payload
    """
    if not supabase_client:
        return
    
    try:
        data = {
            "ended_at": datetime.utcnow().isoformat(),
            "status": "completed",
            "chat_ended_payload": full_payload
        }
        
        result = supabase_client.table("call_sessions").update(data).eq("chat_id", chat_id).execute()
        print(f"[SUPABASE] Logged call session end: {chat_id}")
        return result
    except Exception as e:
        print(f"[SUPABASE ERROR] Failed to log call session end: {e}")
        return None

async def log_tool_call_event(
    chat_id: str,
    tool_call_id: str,
    tool_name: str,
    tool_type: str,
    parameters: dict,
    response_required: bool,
    webhook_payload: dict,
    sequence_number: int = None
):
    """
    Log a tool call event to Supabase.
    
    Args:
        chat_id: Chat ID
        tool_call_id: Unique tool call ID
        tool_name: Name of the tool
        tool_type: Type of tool (function, etc.)
        parameters: Tool parameters
        response_required: Whether response is required
        webhook_payload: Complete webhook payload
        sequence_number: Sequence number of this tool call
    
    Returns:
        The created record ID
    """
    if not supabase_client:
        return None
    
    try:
        data = {
            "chat_id": chat_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "tool_type": tool_type,
            "parameters": parameters,
            "response_required": response_required,
            "called_at": datetime.utcnow().isoformat(),
            "execution_started_at": datetime.utcnow().isoformat(),
            "webhook_payload": webhook_payload,
            "sequence_number": sequence_number
        }
        
        result = supabase_client.table("tool_call_events").insert(data).execute()
        
        if result.data and len(result.data) > 0:
            record_id = result.data[0].get("id")
            print(f"[SUPABASE] Logged tool call event: {tool_name} (ID: {record_id})")
            return record_id
        return None
    except Exception as e:
        print(f"[SUPABASE ERROR] Failed to log tool call event: {e}")
        return None

async def log_tool_call_result(
    tool_call_id: str,
    success: bool,
    result_summary: str = None,
    result_data: dict = None,
    error_type: str = None,
    error_message: str = None,
    error_detail: dict = None,
    response_type: str = None,
    response_content: str = None,
    execution_time_ms: int = None
):
    """
    Update a tool call event with execution results.
    
    Args:
        tool_call_id: Unique tool call ID
        success: Whether the tool execution succeeded
        result_summary: Brief summary of result
        result_data: Complete result data
        error_type: Type of error if failed
        error_message: Error message if failed
        error_detail: Detailed error info
        response_type: Type of response sent to Hume
        response_content: Content sent to Hume
        execution_time_ms: Execution time in milliseconds
    """
    if not supabase_client:
        return
    
    try:
        data = {
            "execution_completed_at": datetime.utcnow().isoformat(),
            "success": success,
            "execution_time_ms": execution_time_ms
        }
        
        if result_summary:
            data["result_summary"] = result_summary
        if result_data:
            data["result_data"] = result_data
        if error_type:
            data["error_type"] = error_type
        if error_message:
            data["error_message"] = error_message
        if error_detail:
            data["error_detail"] = error_detail
        if response_type:
            data["response_type"] = response_type
        if response_content:
            data["response_content"] = response_content
        if response_content:
            data["response_sent_at"] = datetime.utcnow().isoformat()
        
        result = supabase_client.table("tool_call_events").update(data).eq("tool_call_id", tool_call_id).execute()
        print(f"[SUPABASE] Updated tool call result: {tool_call_id} (success={success})")
        return result
    except Exception as e:
        print(f"[SUPABASE ERROR] Failed to log tool call result: {e}")
        return None

async def logged_httpx_request(method: str, url: str, **kwargs):
    """
    Wrapper for httpx requests.
    
    Args:
        method: HTTP method (GET, POST, PATCH, etc.)
        url: Request URL
        **kwargs: Additional arguments for httpx (params, json, headers, timeout, etc.)
    
    Returns:
        httpx.Response object
    """
    async with httpx.AsyncClient() as client:
        if method.upper() == 'GET':
            response = await client.get(url, **kwargs)
        elif method.upper() == 'POST':
            response = await client.post(url, **kwargs)
        elif method.upper() == 'PATCH':
            response = await client.patch(url, **kwargs)
        elif method.upper() == 'PUT':
            response = await client.put(url, **kwargs)
        elif method.upper() == 'DELETE':
            response = await client.delete(url, **kwargs)
        else:
            response = await client.request(method, url, **kwargs)
    
    return response

# =====================================================
# END SUPABASE LOGGING FUNCTIONS
# =====================================================

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

# Syncronizer.io API functions for tool calls
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

async def get_patient_by_id(patient_id):
    """
    Get patient details by ID from the Syncronizer.io API.
    
    Args:
        patient_id: The patient ID
    
    Returns:
        Patient data including phone number, or None if not found
    """
    try:
        bearer_token = await get_bearer_token()
        if not bearer_token:
            print("[GET PATIENT] Authentication failed")
            return None
        
        headers = {
            "Accept": "application/vnd.Nexhealth+json;version=2",
            "Authorization": f"Bearer {bearer_token}"
        }
        
        params = {
            "subdomain": SYNCRONIZER_SUBDOMAIN
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SYNCRONIZER_BASE_URL}/patients/{patient_id}",
                params=params,
                headers=headers,
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                patient = data.get("data", {})
                
                # Extract phone number from bio
                bio = patient.get("bio", {})
                phone = bio.get("cell_phone_number") or bio.get("phone_number") or bio.get("home_phone_number")
                
                return {
                    "id": patient.get("id"),
                    "first_name": patient.get("first_name"),
                    "last_name": patient.get("last_name"),
                    "phone_number": phone,
                    "email": patient.get("email")
                }
            else:
                print(f"[GET PATIENT] Failed to get patient {patient_id}: {response.status_code}")
                return None
                
    except Exception as e:
        print(f"[GET PATIENT] Error: {e}")
        return None

async def get_reminder_context(appointment_id: str):
    """
    Get reminder context for an outbound call.
    Looks up appointment details from Supabase and patient details from NexHealth.
    
    Args:
        appointment_id: The appointment ID (passed as custom_session_id from Hume)
    
    Returns:
        dict with patient_name, appointment_time, provider_name, or error
    """
    print(f"[REMINDER CONTEXT] Looking up context for appointment: {appointment_id}")
    
    if not supabase_client:
        print("[REMINDER CONTEXT] Supabase client not available")
        return {
            "success": False,
            "error": "Database not available"
        }
    
    try:
        # 1. Look up the outbound_calls table for this appointment
        response = supabase_client.table("outbound_calls") \
            .select("*") \
            .eq("appointment_id", appointment_id) \
            .single() \
            .execute()
        
        if not response.data:
            print(f"[REMINDER CONTEXT] No outbound call record found for appointment {appointment_id}")
            return {
                "success": False,
                "error": "Appointment not found in our records"
            }
        
        call_record = response.data
        patient_id = call_record.get("patient_id")
        provider_id = call_record.get("provider_id")
        appointment_time_str = call_record.get("appointment_time")
        timezone_str = call_record.get("timezone", "America/New_York")
        
        print(f"[REMINDER CONTEXT] Found record - Patient ID: {patient_id}, Provider ID: {provider_id}, Time: {appointment_time_str}, TZ: {timezone_str}")
        
        # 2. Get patient details from NexHealth
        patient = await get_patient_by_id(patient_id)
        
        if patient:
            patient_name = f"{patient.get('first_name', '')} {patient.get('last_name', '')}".strip()
        else:
            patient_name = "Patient"
            print(f"[REMINDER CONTEXT] Could not fetch patient details for ID {patient_id}")
        
        # 3. Format appointment time nicely
        from datetime import datetime
        from zoneinfo import ZoneInfo
        
        try:
            # Parse the UTC time from the database
            dt_utc = datetime.fromisoformat(appointment_time_str.replace('Z', '+00:00'))
            
            # Convert to local timezone
            local_tz = ZoneInfo(timezone_str)
            dt_local = dt_utc.astimezone(local_tz)
            
            # Format nicely for speech
            formatted_time = dt_local.strftime("%A, %B %d at %I:%M %p")
            # Remove leading zero from hour (e.g., "09:00 AM" -> "9:00 AM")
            formatted_time = formatted_time.replace(" 0", " ").replace(":00 ", " ")
        except Exception as time_error:
            print(f"[REMINDER CONTEXT] Error formatting time: {time_error}")
            formatted_time = "your upcoming appointment"
        
        # 4. Look up provider name from NexHealth
        provider_name = "your dentist"  # Default fallback
        if provider_id:
            try:
                providers_result = await get_providers(location_id=SYNCRONIZER_LOCATION_ID)
                if providers_result["success"] and providers_result["providers"]:
                    for provider in providers_result["providers"]:
                        if str(provider.get("id")) == str(provider_id):
                            provider_name = provider.get("name", "your dentist")
                            print(f"[REMINDER CONTEXT] Found provider: {provider_name}")
                            break
            except Exception as provider_err:
                print(f"[REMINDER CONTEXT] Error looking up provider: {provider_err}")
        
        print(f"[REMINDER CONTEXT] Returning - Patient: {patient_name}, Time: {formatted_time}")
        
        return {
            "success": True,
            "patient_name": patient_name,
            "patient_first_name": patient.get('first_name', 'there') if patient else 'there',
            "appointment_time": formatted_time,
            "provider_name": provider_name,
            "appointment_id": appointment_id
        }
        
    except Exception as e:
        print(f"[REMINDER CONTEXT] Error: {e}")
        return {
            "success": False,
            "error": str(e)
        }

def make_outbound_call(to_number: str, patient_id: str = None, appointment_id: str = None):
    """
    Make an outbound call using Twilio to connect the patient with Hume EVI.
    
    Args:
        to_number: Phone number to call in E.164 format (e.g., +15163042196)
        patient_id: Optional patient ID for context
        appointment_id: Optional appointment ID for context
    
    Returns:
        dict with call status and details, or error information
    """
    if not twilio_client:
        print("[OUTBOUND CALL] Twilio client not initialized")
        return {
            "success": False,
            "error": "Twilio client not initialized"
        }
    
    try:
        # Format phone number to E.164 if needed
        formatted_number = to_number.strip()
        if not formatted_number.startswith('+'):
            # Assume US number if no country code
            formatted_number = '+1' + ''.join(filter(str.isdigit, formatted_number))
        
        # Build webhook URL with OUTBOUND config (different system prompt for reminders)
        webhook_url = f"https://api.hume.ai/v0/evi/twilio?config_id={HUME_OUTBOUND_CONFIG_ID}&api_key={HUME_API_KEY}"
        
        # Build statusCallback URL - Twilio will POST status updates here
        # We pass appointment_id as a query parameter so we know which call it is
        status_callback_url = f"{VERCEL_URL}/twilio-status"
        if appointment_id:
            status_callback_url += f"?appointment_id={appointment_id}"
        
        print(f"[OUTBOUND CALL] Calling {formatted_number} from {TWILIO_PHONE_NUMBER}")
        print(f"[OUTBOUND CALL] Appointment ID: {appointment_id}")
        print(f"[OUTBOUND CALL] Status callback URL: {status_callback_url}")
        
        # Make the call with statusCallback to track when call is answered/completed
        call = twilio_client.calls.create(
            to=formatted_number,
            from_=TWILIO_PHONE_NUMBER,
            url=webhook_url,
            status_callback=status_callback_url,
            status_callback_event=['answered', 'completed'],
            status_callback_method='POST'
        )
        
        print(f"[OUTBOUND CALL] Call initiated - SID: {call.sid}, Status: {call.status}")
        
        return {
            "success": True,
            "call_sid": call.sid,
            "status": call.status,
            "to": formatted_number,
            "from": TWILIO_PHONE_NUMBER
        }
        
    except Exception as e:
        print(f"[OUTBOUND CALL ERROR] Failed to make call: {e}")
        return {
            "success": False,
            "error": str(e)
        }

async def process_pending_outbound_calls(hours_before: int = 24, calling_hours: tuple = (9, 19)):
    """
    Process pending outbound calls from the queue.
    This function is designed to be called by a cron job.
    
    Args:
        hours_before: Hours before appointment to make the call (default: 24)
        calling_hours: Tuple of (start_hour, end_hour) in local time (default: 9 AM to 7 PM)
    
    Returns:
        dict with processing results
    """
    if not supabase_client:
        return {"success": False, "error": "Supabase client not initialized"}
    
    if not twilio_client:
        return {"success": False, "error": "Twilio client not initialized"}
    
    try:
        from zoneinfo import ZoneInfo
        
        # Get pending calls that are due (appointment within next X hours)
        result = supabase_client.table("outbound_calls").select("*").eq(
            "status", "pending"
        ).execute()
        
        pending_calls = result.data or []
        processed = 0
        skipped = 0
        failed = 0
        
        for call_record in pending_calls:
            try:
                # Parse appointment time
                appt_time = datetime.fromisoformat(call_record['appointment_time'].replace('Z', '+00:00'))
                timezone_str = call_record.get('timezone', 'America/New_York')
                tz = ZoneInfo(timezone_str)
                
                # Convert to local time
                now_local = datetime.now(tz)
                appt_local = appt_time.astimezone(tz)
                
                # Check if appointment is within the reminder window
                hours_until_appt = (appt_local - now_local).total_seconds() / 3600
                
                # Test mode bypasses all time checks
                if OUTBOUND_TEST_MODE:
                    print(f"[CRON TEST MODE] Bypassing time checks for appointment {call_record['appointment_id']}")
                else:
                    if hours_until_appt > hours_before or hours_until_appt < 0:
                        print(f"[CRON] Skipping appointment {call_record['appointment_id']} - hours_until_appt: {hours_until_appt}")
                        continue  # Not due yet or already passed
                    
                    # Check if current time is within calling hours
                    current_hour = now_local.hour
                    if current_hour < calling_hours[0] or current_hour >= calling_hours[1]:
                        skipped += 1
                        print(f"[CRON] Skipping - outside calling hours ({current_hour} not in {calling_hours})")
                        continue  # Outside calling hours
                
                # Status stays 'pending' until Twilio confirms call was answered
                # Twilio's statusCallback will update to 'in_progress' when answered
                # and 'completed' when the call ends
                
                # Make the call
                call_result = make_outbound_call(
                    to_number=call_record['phone_number'],
                    patient_id=call_record['patient_id'],
                    appointment_id=call_record['appointment_id']
                )
                
                # Update the record based on result
                if call_result['success']:
                    # Mark as 'calling' - Twilio's statusCallback will update to 'in_progress' when answered
                    supabase_client.table("outbound_calls").update({
                        "status": "calling",  # Intermediate status: call initiated but not answered yet
                        "call_sid": call_result.get('call_sid'),  # Store Twilio's call SID
                        "call_attempts": call_record.get('call_attempts', 0) + 1,
                        "last_attempt_at": datetime.utcnow().isoformat(),
                        "updated_at": datetime.utcnow().isoformat()
                    }).eq("appointment_id", call_record['appointment_id']).execute()
                    processed += 1
                    print(f"[CRON] Call initiated for {call_record['appointment_id']} - status: calling, SID: {call_result.get('call_sid')}")
                else:
                    supabase_client.table("outbound_calls").update({
                        "status": "failed" if call_record.get('call_attempts', 0) >= 2 else "pending",
                        "call_attempts": call_record.get('call_attempts', 0) + 1,
                        "last_attempt_at": datetime.utcnow().isoformat(),
                        "updated_at": datetime.utcnow().isoformat()
                    }).eq("appointment_id", call_record['appointment_id']).execute()
                    failed += 1
                    
            except Exception as call_err:
                print(f"[OUTBOUND CALL ERROR] Failed to process call {call_record.get('appointment_id')}: {call_err}")
                failed += 1
        
        return {
            "success": True,
            "processed": processed,
            "skipped": skipped,
            "failed": failed,
            "total_pending": len(pending_calls)
        }
        
    except Exception as e:
        print(f"[OUTBOUND CALL ERROR] Failed to process pending calls: {e}")
        return {"success": False, "error": str(e)}

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
                    patient_id = patient.get("id")
                    print(f"[SEARCH DEBUG] Raw patient data - ID: {patient_id}, First: {patient.get('first_name')}, Last: {patient.get('last_name')}")
                    formatted_patient = {
                        "id": patient_id,
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

async def get_patient_appointments(patient_id, start_date=None, end_date=None, cancelled=False):
    """
    Get appointments for a specific patient.
    
    Args:
        patient_id: Patient ID (required)
        start_date: Start date for search in YYYY-MM-DD format (optional, defaults to today)
        end_date: End date for search in YYYY-MM-DD format (optional, defaults to 90 days from start)
        cancelled: Include cancelled appointments (default: False)
    
    Returns:
        List of appointments or error message
    """
    try:
        # Get valid bearer token
        bearer_token = await get_bearer_token()
        if not bearer_token:
            return {
                "success": False,
                "message": "Authentication failed. Unable to access appointments.",
                "appointments": []
            }
        
        # Set default date range if not provided
        from datetime import datetime, timedelta
        if not start_date:
            start_date = datetime.now().strftime("%Y-%m-%d")
        if not end_date:
            # Default to 90 days from start
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = start_dt + timedelta(days=90)
            end_date = end_dt.strftime("%Y-%m-%d")
        
        # Convert to ISO format with timezone
        start_iso = f"{start_date}T00:00:00+00:00"
        end_iso = f"{end_date}T23:59:59+00:00"
        
        # Prepare query parameters
        params = {
            "subdomain": SYNCRONIZER_SUBDOMAIN,
            "location_id": SYNCRONIZER_LOCATION_ID,
            "patient_id": patient_id,
            "start": start_iso,
            "end": end_iso,
            "cancelled": str(cancelled).lower(),
            "per_page": 50  # Get up to 50 appointments
        }
        
        # Set up headers
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {bearer_token}",
            "Nex-Api-Version": "v20240412"
        }
        
        print(f"[APPOINTMENTS] Fetching appointments for patient {patient_id} from {start_date} to {end_date}")
        
        # Make API request
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SYNCRONIZER_BASE_URL}/appointments",
                params=params,
                headers=headers,
                timeout=10.0
            )
            
            print(f"[APPOINTMENTS] Response status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                appointments_data = data.get("data", [])
                
                print(f"[APPOINTMENTS] Found {len(appointments_data)} appointment(s)")
                
                # Format appointments for voice agent
                formatted_appointments = []
                for appt in appointments_data:
                    formatted_appt = {
                        "id": appt.get("id"),
                        "patient_id": appt.get("patient_id"),
                        "provider_id": appt.get("provider_id"),
                        "provider_name": appt.get("provider_name", "Unknown Provider"),
                        "start_time": appt.get("start_time"),
                        "end_time": appt.get("end_time"),
                        "timezone": appt.get("timezone", "America/New_York"),
                        "confirmed": appt.get("confirmed", False),
                        "cancelled": appt.get("cancelled", False),
                        "note": appt.get("note", ""),
                        "location_id": appt.get("location_id")
                    }
                    formatted_appointments.append(formatted_appt)
                    print(f"[APPOINTMENTS] Appt {appt.get('id')}: {appt.get('start_time')} with {appt.get('provider_name')}")
                
                return {
                    "success": True,
                    "message": f"Found {len(formatted_appointments)} appointment(s)",
                    "appointments": formatted_appointments
                }
            else:
                error_detail = response.text
                print(f"[APPOINTMENTS ERROR] {response.status_code}: {error_detail}")
                return {
                    "success": False,
                    "message": f"Failed to get appointments. API error: {response.status_code}",
                    "appointments": []
                }
                
    except httpx.TimeoutException:
        return {
            "success": False,
            "message": "Request timed out while fetching appointments.",
            "appointments": []
        }
    except Exception as e:
        print(f"[APPOINTMENTS EXCEPTION] {str(e)}")
        return {
            "success": False,
            "message": f"Error fetching appointments: {str(e)}",
            "appointments": []
        }

async def create_patient(first_name, last_name, date_of_birth, email, phone_number, middle_name=None, address=None):
    """
    Create a new patient in the Syncronizer.io system.
    
    Args:
        first_name: Patient's first name (required)
        last_name: Patient's last name (required)
        date_of_birth: Patient's DOB in YYYY-MM-DD format (required)
        email: Patient's email address (required)
        phone_number: Patient's phone number (required)
        middle_name: Patient's middle name (optional)
        address: Patient's address dict with street_address, city, state, zip_code (optional)
    
    Returns:
        Created patient data or error message
    """
    try:
        # Get valid bearer token
        bearer_token = await get_bearer_token()
        if not bearer_token:
            return {
                "success": False,
                "message": "Authentication failed. Unable to create patient record.",
                "patient": None
            }
        
        # Clean phone number (remove spaces, dashes, parentheses)
        clean_phone = ''.join(filter(str.isdigit, phone_number))
        
        # Get a default provider ID - we'll fetch the first available provider
        # This is required by the API for patient creation
        providers_result = await get_providers(location_id=SYNCRONIZER_LOCATION_ID)
        provider_id = None
        if providers_result["success"] and providers_result["providers"]:
            provider_id = providers_result["providers"][0]["id"]
            print(f"[CREATE PATIENT] Using provider ID: {provider_id}")
        
        # Build request body with proper nested JSON structure
        # The API expects proper JSON with nested objects
        bio_data = {
            "date_of_birth": date_of_birth,
            "phone_number": clean_phone
        }
        
        # Add optional address fields to bio
        if address and isinstance(address, dict):
            if address.get("street_address"):
                bio_data["street_address"] = address["street_address"]
            if address.get("city"):
                bio_data["city"] = address["city"]
            if address.get("state"):
                bio_data["state"] = address["state"]
            if address.get("zip_code"):
                bio_data["zip_code"] = address["zip_code"]
        
        # Build patient object
        patient_data = {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "bio": bio_data
        }
        
        # Add optional middle name
        if middle_name:
            patient_data["middle_name"] = middle_name
        
        # Build complete request body
        request_body = {
            "patient": patient_data
        }
        
        # Add provider if available
        if provider_id:
            request_body["provider"] = {
                "provider_id": int(provider_id)
            }
        
        # Prepare query parameters - location_id is required
        params = {
            "subdomain": SYNCRONIZER_SUBDOMAIN,
            "location_id": SYNCRONIZER_LOCATION_ID
        }
        
        # Set up headers with bearer token
        # API expects Accept header in format: application/vnd.Nexhealth+json;version=2
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/vnd.Nexhealth+json;version=2",
            "Authorization": f"Bearer {bearer_token}"
        }
        
        print(f"[CREATE PATIENT] Creating patient: {first_name} {last_name}, DOB: {date_of_birth}")
        print(f"[CREATE PATIENT] Request body: {request_body}")
        
        # Make API request with JSON body
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SYNCRONIZER_BASE_URL}/patients",
                params=params,
                json=request_body,  # Use JSON instead of form data
                headers=headers,
                timeout=10.0
            )
            
            print(f"[CREATE PATIENT] Response status: {response.status_code}")
            
            if response.status_code in [200, 201]:
                data = response.json()
                print(f"[CREATE PATIENT] Response received successfully")
                
                # Patient data is nested under data.user
                patient = data.get("data", {}).get("user", {})
                bio = patient.get("bio", {}) if isinstance(patient.get("bio"), dict) else {}
                
                # Format patient info for voice response
                formatted_patient = {
                    "id": patient.get("id"),
                    "name": patient.get("name") or f"{patient.get('first_name', '')} {patient.get('last_name', '')}".strip(),
                    "first_name": patient.get("first_name"),
                    "last_name": patient.get("last_name"),
                    "date_of_birth": bio.get("date_of_birth"),
                    "phone": bio.get("phone_number"),
                    "email": patient.get("email")
                }
                print(f"[CREATE PATIENT] Patient created: ID={formatted_patient['id']}, Name={formatted_patient['name']}")
                
                return {
                    "success": True,
                    "message": f"Successfully created patient record for {formatted_patient['name']}.",
                    "patient": formatted_patient
                }
            
            else:
                error_detail = response.text
                print(f"[CREATE PATIENT ERROR] {response.status_code}: {error_detail}")
                return {
                    "success": False,
                    "message": f"Failed to create patient. API error: {response.status_code}",
                    "patient": None,
                    "error_detail": error_detail
                }
                
    except httpx.TimeoutException:
        return {
            "success": False,
            "message": "Request timed out while creating patient. Please try again.",
            "patient": None
        }
    except Exception as e:
        print(f"[CREATE PATIENT EXCEPTION] {str(e)}")
        return {
            "success": False,
            "message": f"Error creating patient: {str(e)}",
            "patient": None
        }

async def get_operatories(location_id=None):
    """
    Get operatories (treatment rooms/chairs) from the Syncronizer.io API.
    
    Args:
        location_id: Filter by specific location (optional)
    
    Returns:
        List of operatories or error message
    """
    try:
        # Get valid bearer token
        bearer_token = await get_bearer_token()
        if not bearer_token:
            return {
                "success": False,
                "message": "Authentication failed. Unable to access operatory information.",
                "operatories": []
            }
        
        # Prepare query parameters
        params = {
            "subdomain": SYNCRONIZER_SUBDOMAIN,
            "per_page": 50  # Get all operatories
        }
        
        # Add location filter
        if location_id:
            params["location_id"] = location_id
        else:
            params["location_id"] = SYNCRONIZER_LOCATION_ID
        
        # Set up headers
        headers = {
            "Accept": "application/vnd.Nexhealth+json;version=2",
            "Authorization": f"Bearer {bearer_token}"
        }
        
        print(f"[OPERATORIES] Fetching operatories for location {params['location_id']}")
        
        # Make API request
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SYNCRONIZER_BASE_URL}/operatories",
                params=params,
                headers=headers,
                timeout=10.0
            )
            
            print(f"[OPERATORIES] Response status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                operatories_data = data.get("data", [])
                
                # Format operatories for easier use
                operatories = []
                for op in operatories_data:
                    # Only include active and bookable operatories
                    if op.get("active", False) and op.get("bookable_online", False):
                        operatories.append({
                            "id": op.get("id"),
                            "name": op.get("name"),
                            "display_name": op.get("display_name"),
                            "location_id": op.get("location_id")
                        })
                
                print(f"[OPERATORIES] Found {len(operatories)} active bookable operatories")
                
                return {
                    "success": True,
                    "message": f"Found {len(operatories)} operatories",
                    "operatories": operatories
                }
            else:
                error_detail = response.text
                print(f"[OPERATORIES ERROR] {response.status_code}: {error_detail}")
                return {
                    "success": False,
                    "message": f"Failed to get operatories. API error: {response.status_code}",
                    "operatories": []
                }
                
    except httpx.TimeoutException:
        return {
            "success": False,
            "message": "Request timed out while fetching operatories.",
            "operatories": []
        }
    except Exception as e:
        print(f"[OPERATORIES EXCEPTION] {str(e)}")
        return {
            "success": False,
            "message": f"Error fetching operatories: {str(e)}",
            "operatories": []
        }

async def book_appointment(patient_id, provider_id, start_time, end_time=None, appointment_type_id=None, operatory_id=None, note=None, notify_patient=True):
    """
    Book/create an appointment in the NexHealth system.
    
    Args:
        patient_id: ID of the patient (required)
        provider_id: ID of the provider (required)
        start_time: Appointment start time in ISO format (required) e.g., "2024-12-15T14:30:00Z"
        end_time: Appointment end time in ISO format (optional, will be calculated if not provided)
        appointment_type_id: ID of appointment type (optional)
        operatory_id: ID of operatory/treatment room (optional but required by some locations)
        note: Notes about the appointment (optional)
        notify_patient: Whether to send notification to patient (default: True)
    
    Returns:
        Created appointment data or error message
    """
    try:
        # Get valid bearer token
        bearer_token = await get_bearer_token()
        if not bearer_token:
            return {
                "success": False,
                "message": "Authentication failed. Unable to book appointment.",
                "appointment": None
            }
        
        # If no operatory_id provided, try to get one automatically
        if not operatory_id:
            print(f"[BOOK APPOINTMENT] No operatory_id provided, fetching available operatories...")
            operatories_result = await get_operatories(location_id=SYNCRONIZER_LOCATION_ID)
            if operatories_result["success"] and operatories_result["operatories"]:
                operatory_id = operatories_result["operatories"][0]["id"]
                print(f"[BOOK APPOINTMENT] Using operatory ID: {operatory_id}")
            else:
                print(f"[BOOK APPOINTMENT WARNING] Could not fetch operatory, proceeding without it")
        
        # Build appointment request body
        appt_data = {
            "patient_id": int(patient_id),
            "provider_id": int(provider_id),
            "start_time": start_time
        }
        
        # Add optional fields
        if end_time:
            appt_data["end_time"] = end_time
        
        if appointment_type_id:
            appt_data["appointment_type_id"] = int(appointment_type_id)
        
        if operatory_id:
            appt_data["operatory_id"] = int(operatory_id)
        
        if note:
            appt_data["note"] = note
        
        # Build complete request body
        request_body = {
            "appt": appt_data
        }
        
        # Prepare query parameters
        params = {
            "subdomain": SYNCRONIZER_SUBDOMAIN,
            "location_id": SYNCRONIZER_LOCATION_ID,
            "notify_patient": str(notify_patient).lower()
        }
        
        # Set up headers
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/vnd.Nexhealth+json;version=2",
            "Authorization": f"Bearer {bearer_token}"
        }
        
        print(f"[BOOK APPOINTMENT] Creating appointment for patient {patient_id} with provider {provider_id}")
        print(f"[BOOK APPOINTMENT] Start time: {start_time}")
        print(f"[BOOK APPOINTMENT] Request body: {request_body}")
        
        # Make API request
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SYNCRONIZER_BASE_URL}/appointments",
                params=params,
                json=request_body,
                headers=headers,
                timeout=10.0
            )
            
            print(f"[BOOK APPOINTMENT] Response status: {response.status_code}")
            
            if response.status_code in [200, 201]:
                data = response.json()
                print(f"[BOOK APPOINTMENT] Response received successfully")
                
                # Appointment data is nested under data.appt
                appointment = data.get("data", {}).get("appt", {})
                patient_data = appointment.get("patient", {})
                
                # Format appointment info for voice response
                formatted_appointment = {
                    "id": appointment.get("id"),
                    "patient_id": appointment.get("patient_id"),
                    "patient_name": patient_data.get("name", ""),
                    "provider_id": appointment.get("provider_id"),
                    "provider_name": appointment.get("provider_name", ""),
                    "start_time": appointment.get("start_time"),
                    "end_time": appointment.get("end_time"),
                    "confirmed": appointment.get("confirmed", False),
                    "note": appointment.get("note", ""),
                    "location_id": appointment.get("location_id")
                }
                
                print(f"[BOOK APPOINTMENT] Appointment created: ID={formatted_appointment['id']}, Start={formatted_appointment['start_time']}")
                
                return {
                    "success": True,
                    "message": f"Successfully booked appointment for {formatted_appointment['patient_name']} with {formatted_appointment['provider_name']} at {formatted_appointment['start_time']}.",
                    "appointment": formatted_appointment
                }
            
            else:
                error_detail = response.text
                print(f"[BOOK APPOINTMENT ERROR] {response.status_code}: {error_detail}")
                return {
                    "success": False,
                    "message": f"Failed to book appointment. API error: {response.status_code}",
                    "appointment": None,
                    "error_detail": error_detail
                }
                
    except httpx.TimeoutException:
        return {
            "success": False,
            "message": "Request timed out while booking appointment. Please try again.",
            "appointment": None
        }
    except Exception as e:
        print(f"[BOOK APPOINTMENT EXCEPTION] {str(e)}")
        return {
            "success": False,
            "message": f"Error booking appointment: {str(e)}",
            "appointment": None
        }

async def reschedule_appointment(appointment_id, start_time=None, end_time=None, provider_id=None, operatory_id=None, note=None, cancelled=False, confirmed=None, notify_patient=True):
    """
    Reschedule or edit an existing appointment in the Syncronizer.io API.
    
    Args:
        appointment_id: The ID of the appointment to edit (required)
        start_time: New start time in timezone-aware format (e.g., "2025-12-12T09:30:00.000-05:00")
        end_time: New end time (optional)
        provider_id: New provider ID (optional)
        operatory_id: New operatory ID (optional)
        note: Updated note for the appointment (optional)
        cancelled: Set to True to cancel the appointment (default: False)
        confirmed: Set appointment confirmation status (optional)
        notify_patient: Whether to send notification to patient (default: True)
    
    Returns:
        Updated appointment details or error message
    """
    try:
        # Get valid bearer token
        bearer_token = await get_bearer_token()
        if not bearer_token:
            return {
                "success": False,
                "message": "Authentication failed. Unable to reschedule appointment.",
                "appointment": None
            }
        
        # Build the appointment update data
        appt_data = {}
        
        if start_time is not None:
            appt_data["start_time"] = start_time
        
        if end_time is not None:
            appt_data["end_time"] = end_time
        
        if provider_id is not None:
            appt_data["provider_id"] = int(provider_id)
        
        if operatory_id is not None:
            appt_data["operatory_id"] = int(operatory_id)
        
        if note is not None:
            appt_data["note"] = note
        
        if cancelled is not None:
            appt_data["cancelled"] = cancelled
        
        if confirmed is not None:
            appt_data["confirmed"] = confirmed
        
        # Validate we have something to update
        if not appt_data:
            return {
                "success": False,
                "message": "No fields specified for update",
                "appointment": None
            }
        
        # Build complete request body
        request_body = {
            "appt": appt_data
        }
        
        # Prepare query parameters
        params = {
            "subdomain": SYNCRONIZER_SUBDOMAIN
        }
        
        # Set up headers
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/vnd.Nexhealth+json;version=2",
            "Authorization": f"Bearer {bearer_token}"
        }
        
        print(f"[RESCHEDULE APPOINTMENT] Updating appointment ID: {appointment_id}")
        print(f"[RESCHEDULE APPOINTMENT] Updates: {appt_data}")
        
        # Make API request (PATCH)
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{SYNCRONIZER_BASE_URL}/appointments/{appointment_id}",
                params=params,
                json=request_body,
                headers=headers,
                timeout=10.0
            )
            
            print(f"[RESCHEDULE APPOINTMENT] Response status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                print(f"[RESCHEDULE APPOINTMENT] Appointment updated successfully")
                
                # Appointment data is nested under data.appt
                appointment = data.get("data", {}).get("appt", {})
                
                return {
                    "success": True,
                    "message": "Appointment updated successfully",
                    "appointment": {
                        "id": appointment.get("id"),
                        "patient_id": appointment.get("patient_id"),
                        "provider_id": appointment.get("provider_id"),
                        "provider_name": appointment.get("provider_name"),
                        "start_time": appointment.get("start_time"),
                        "end_time": appointment.get("end_time"),
                        "timezone": appointment.get("timezone"),
                        "note": appointment.get("note"),
                        "confirmed": appointment.get("confirmed"),
                        "cancelled": appointment.get("cancelled"),
                        "location_id": appointment.get("location_id"),
                        "operatory_id": appointment.get("operatory_id"),
                        "created_at": appointment.get("created_at"),
                        "updated_at": appointment.get("updated_at")
                    }
                }
            else:
                error_data = response.json()
                error_messages = error_data.get("error", [])
                error_text = ", ".join(error_messages) if isinstance(error_messages, list) else str(error_messages)
                
                print(f"[RESCHEDULE APPOINTMENT ERROR] {response.status_code}: {response.text}")
                
                return {
                    "success": False,
                    "message": f"Failed to update appointment: {error_text}",
                    "error_detail": error_text,
                    "appointment": None
                }
    
    except httpx.TimeoutException:
        print(f"[RESCHEDULE APPOINTMENT TIMEOUT] Request timed out")
        return {
            "success": False,
            "message": "Request timed out while updating appointment. Please try again.",
            "appointment": None
        }
    except Exception as e:
        print(f"[RESCHEDULE APPOINTMENT EXCEPTION] {str(e)}")
        return {
            "success": False,
            "message": f"Error updating appointment: {str(e)}",
            "appointment": None
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
        else:
            # Get the dynamic location ID from our locations
            locations_result = await get_locations()
            if locations_result["success"] and locations_result["locations"]:
                dynamic_location_id = locations_result["locations"][0]["id"]
                params["location_id"] = dynamic_location_id
                print(f"[PROVIDERS] Using dynamic location ID: {dynamic_location_id}")
            else:
                # Fallback to configured location
                params["location_id"] = SYNCRONIZER_LOCATION_ID
                print(f"[PROVIDERS] Using fallback location ID: {SYNCRONIZER_LOCATION_ID}")
            
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

async def get_locations(location_name=None, include_inactive=False):
    """
    Get practice locations from the Syncronizer.io API.
    Dynamically fetches locations and finds Green River Dental.
    
    Args:
        location_name: Location name to search for (optional, for filtering results)
        include_inactive: Include inactive locations (optional, default False)
    
    Returns:
        List of locations or error message
    """
    try:
        # Get valid bearer token
        bearer_token = await get_bearer_token()
        if not bearer_token:
            return {
                "success": False,
                "message": "Authentication failed. Unable to access location information.",
                "locations": []
            }
        
        # First, try to get all locations to find our practice
        params = {
            "subdomain": SYNCRONIZER_SUBDOMAIN
        }
        
        if include_inactive:
            params["inactive"] = True
        
        # Set up headers with bearer token
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {bearer_token}",
            "Nex-Api-Version": "v20240412"
        }
        
        print(f"[LOCATIONS] Fetching locations dynamically...")
        
        # Get all locations first
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SYNCRONIZER_BASE_URL}/locations",
                params=params,
                headers=headers,
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                print(f"[LOCATIONS RAW API] Response data keys: {list(data.keys())}")
                print(f"[LOCATIONS RAW API] Data type: {type(data.get('data'))}")
                
                # Handle different possible API response structures
                locations_data = []
                
                # Check if data is directly an array of locations
                if isinstance(data.get("data"), list):
                    print(f"[LOCATIONS] Data is a list, using directly")
                    locations_data = data.get("data", [])
                # Check if data contains an institution with locations
                elif isinstance(data.get("data"), dict):
                    institution_data = data.get("data", {})
                    print(f"[LOCATIONS DEBUG] Institution data keys: {list(institution_data.keys())}")
                    print(f"[LOCATIONS DEBUG] Institution name: {institution_data.get('name')}")
                    print(f"[LOCATIONS DEBUG] Institution ID: {institution_data.get('id')}")
                    print(f"[LOCATIONS DEBUG] Has locations key: {'locations' in institution_data}")
                    
                    if "locations" in institution_data and institution_data["locations"]:
                        # Use the locations INSIDE the institution, not the institution itself
                        locations_data = institution_data["locations"]
                        print(f"[LOCATIONS] ✅ USING LOCATIONS ARRAY: Found {len(locations_data)} location(s) inside institution")
                        for i, loc in enumerate(locations_data):
                            print(f"[LOCATIONS DEBUG] Location {i}: {loc.get('name')} (ID: {loc.get('id')})")
                    else:
                        # ❌ This is the problem - we fall back to using the institution
                        print(f"[LOCATIONS DEBUG] ❌ FALLBACK: No locations array found or empty, using institution as location")
                        print(f"[LOCATIONS DEBUG] Institution locations value: {institution_data.get('locations')}")
                        locations_data = [institution_data]
                else:
                    print(f"[LOCATIONS] Data is neither list nor dict: {type(data.get('data'))}")
                
                print(f"[LOCATIONS] Found {len(locations_data)} location(s) in API response")
                
                # DEBUG: Print what we actually got
                if locations_data:
                    for i, loc in enumerate(locations_data):
                        print(f"[LOCATIONS DEBUG RAW] Location {i}: {loc}")
                
                # If we didn't find locations in the general endpoint, try using our known location ID
                if not locations_data:
                    print(f"[LOCATIONS] No locations in general endpoint, trying specific location {SYNCRONIZER_LOCATION_ID}")
                    specific_response = await client.get(
                        f"{SYNCRONIZER_BASE_URL}/locations/{SYNCRONIZER_LOCATION_ID}",
                        params=params,
                        headers=headers,
                        timeout=10.0
                    )
                    
                    if specific_response.status_code == 200:
                        specific_data = specific_response.json()
                        location_data = specific_data.get("data", {})
                        if location_data:
                            locations_data = [location_data]
                            print(f"[LOCATIONS] Using specific location: {location_data.get('name')} (ID: {location_data.get('id')})")
                
                # Format locations for voice agent
                formatted_locations = []
                
                for i, location in enumerate(locations_data):
                    print(f"[LOCATIONS FORMAT] Processing item {i}: ID={location.get('id')}, name={location.get('name')}")
                    
                    # Check if this looks like a location (ID > 100000) vs institution (ID < 50000)
                    location_id = location.get("id")
                    location_name = location.get("name", "Unknown Location")
                    
                    if location_id and location_id > 100000:
                        print(f"[LOCATIONS FORMAT] ✅ LOOKS LIKE LOCATION: {location_name} (ID: {location_id})")
                    else:
                        print(f"[LOCATIONS FORMAT] ❌ LOOKS LIKE INSTITUTION: {location_name} (ID: {location_id})")
                        # Skip institutions - they shouldn't be in our location list
                        if location_id and location_id < 50000:
                            print(f"[LOCATIONS FORMAT] Skipping institution {location_name}")
                            continue
                    
                    formatted_location = {
                        "id": location_id,
                        "name": location_name,
                        "address": location.get("street_address", ""),
                        "city": location.get("city", ""),
                        "state": location.get("state", ""),
                        "zip_code": location.get("zip_code", ""),
                        "phone": location.get("phone_number", ""),
                        "inactive": location.get("inactive", False)
                    }
                    
                    # Skip inactive locations unless requested
                    if not include_inactive and formatted_location["inactive"]:
                        print(f"[LOCATIONS FORMAT] Skipping inactive location {location_name}")
                        continue
                        
                    formatted_locations.append(formatted_location)
                    print(f"[LOCATIONS FORMAT] ✅ Added location: {formatted_location['name']} (ID: {formatted_location['id']})")
                
                # Filter by location name if specified
                if location_name and formatted_locations:
                    search_name = location_name.lower()
                    filtered_locations = []
                    
                    for location in formatted_locations:
                        location_full_name = location['name'].lower()
                        location_address = f"{location['address']} {location['city']}".lower()
                        
                        if (search_name in location_full_name or 
                            search_name in location_address or
                            any(search_name in word for word in location_full_name.split())):
                            filtered_locations.append(location)
                    
                    formatted_locations = filtered_locations
                
                if formatted_locations:
                    # Log the found location for debugging
                    main_location = formatted_locations[0]
                    print(f"[LOCATIONS FINAL] Returning location: {main_location['name']} (ID: {main_location['id']})")
                    print(f"[LOCATIONS FINAL] Expected Green River Dental (ID: 334724)")
                    
                    return {
                        "success": True,
                        "message": f"Found {len(formatted_locations)} location(s).",
                        "locations": formatted_locations,
                        "total_count": len(formatted_locations)
                    }
                else:
                    print(f"[LOCATIONS] No formatted locations found, using specific location API call")
                    # Try to get the specific location we know exists
                    try:
                        specific_response = await client.get(
                            f"{SYNCRONIZER_BASE_URL}/locations/{SYNCRONIZER_LOCATION_ID}",
                            params=params,
                            headers=headers,
                            timeout=10.0
                        )
                        
                        if specific_response.status_code == 200:
                            specific_data = specific_response.json()
                            location_data = specific_data.get("data", {})
                            if location_data and location_data.get("id") == SYNCRONIZER_LOCATION_ID:
                                formatted_location = {
                                    "id": location_data.get("id"),
                                    "name": location_data.get("name", "Green River Dental"),
                                    "address": location_data.get("street_address", "428 Broadway"),
                                    "city": location_data.get("city", "New York"),
                                    "state": location_data.get("state", "NY"),
                                    "zip_code": location_data.get("zip_code", "10013"),
                                    "phone": location_data.get("phone_number", "2222222222"),
                                    "inactive": location_data.get("inactive", False)
                                }
                                print(f"[LOCATIONS SPECIFIC] Got correct location: {formatted_location['name']} (ID: {formatted_location['id']})")
                                return {
                                    "success": True,
                                    "message": f"Found location: {formatted_location['name']}",
                                    "locations": [formatted_location],
                                    "total_count": 1
                                }
                    except Exception as e:
                        print(f"[LOCATIONS] Error getting specific location: {e}")
                    
                    # Final fallback
                    fallback_location = {
                        "id": SYNCRONIZER_LOCATION_ID,
                        "name": "Green River Dental",
                        "address": "428 Broadway",
                        "city": "New York",
                        "state": "NY",
                        "zip_code": "10013", 
                        "phone": "2222222222",
                        "inactive": False
                    }
                    print(f"[LOCATIONS FALLBACK] Using hardcoded location: {fallback_location['name']} (ID: {fallback_location['id']})")
                    
                    return {
                        "success": True,
                        "message": f"Found location: {fallback_location['name']} (using fallback data)",
                        "locations": [fallback_location],
                        "total_count": 1
                    }
            
            else:
                print(f"[LOCATIONS] API error {response.status_code}: {response.text}")
                # API error - return fallback location
                fallback_location = {
                    "id": SYNCRONIZER_LOCATION_ID,
                    "name": "Green River Dental", 
                    "address": "428 Broadway",
                    "city": "New York",
                    "state": "NY",
                    "zip_code": "10013",
                    "phone": "2222222222",
                    "inactive": False
                }
                
                return {
                    "success": True,
                    "message": f"Found location: {fallback_location['name']} (using cached data)",
                    "locations": [fallback_location],
                    "total_count": 1
                }
                
    except Exception as e:
        # Fallback to known location if API fails
        print(f"[LOCATIONS] Exception occurred, using fallback: {str(e)}")
        
        fallback_location = {
            "id": SYNCRONIZER_LOCATION_ID,
            "name": "Green River Dental",
            "address": "428 Broadway", 
            "city": "New York",
            "state": "NY",
            "zip_code": "10013",
            "phone": "2222222222",
            "inactive": False
        }
        
        return {
            "success": True,
            "message": f"Found location: {fallback_location['name']}",
            "locations": [fallback_location],
            "total_count": 1
        }

async def get_available_slots(start_date, days, provider_ids=None, location_ids=None, appointment_type_id=None, slot_length=None):
    """
    Get available appointment slots from the Syncronizer.io API.
    
    Args:
        start_date: Start date in YYYY-MM-DD format (required)
        days: Number of days to search (required)
        provider_ids: List of provider IDs to search (optional, defaults to all)
        location_ids: List of location IDs to search (optional, defaults to configured location)
        appointment_type_id: Specific appointment type ID (optional)
        slot_length: Override default slot length in minutes (optional)
    
    Returns:
        Available slots or error message
    """
    try:
        # Get valid bearer token
        bearer_token = await get_bearer_token()
        if not bearer_token:
            return {
                "success": False,
                "message": "Authentication failed. Unable to check availability.",
                "slots": []
            }
        
        # Prepare query parameters - all required params
        params = {
            "subdomain": SYNCRONIZER_SUBDOMAIN,
            "start_date": start_date,
            "days": days
        }
        
        # Handle location IDs - required as array (API expects lids[] format)
        if location_ids:
            # Convert single location to list if needed
            if isinstance(location_ids, int):
                location_ids = [location_ids]
            # For httpx, we need to pass multiple values as a list for the same key
            params["lids[]"] = location_ids
        else:
            # Get the dynamic location ID from our locations
            locations_result = await get_locations()
            if locations_result["success"] and locations_result["locations"]:
                dynamic_location_id = locations_result["locations"][0]["id"]
                params["lids[]"] = [dynamic_location_id]  # Always pass as list
                print(f"[SLOTS] Using dynamic location ID: {dynamic_location_id}")
            else:
                # Fallback to configured location
                params["lids[]"] = [SYNCRONIZER_LOCATION_ID]  # Always pass as list
                print(f"[SLOTS] Using fallback location ID: {SYNCRONIZER_LOCATION_ID}")
        
        # Handle provider IDs - required as array (API expects pids[] format)  
        if provider_ids:
            # Convert single provider to list if needed
            if isinstance(provider_ids, int):
                provider_ids = [provider_ids]
            # For httpx, we need to pass multiple values as a list for the same key
            params["pids[]"] = provider_ids
        else:
            # If no specific providers requested, we need to get all requestable providers
            providers_result = await get_providers(requestable=True)
            if providers_result["success"] and providers_result["providers"]:
                available_provider_ids = [p["id"] for p in providers_result["providers"]]
                params["pids[]"] = available_provider_ids[:3]  # Limit to first 3 providers
                print(f"[SLOTS] Using {len(available_provider_ids[:3])} requestable provider IDs")
            else:
                return {
                    "success": False,
                    "message": "No available providers found for scheduling.",
                    "slots": []
                }
        
        # Add optional parameters
        if appointment_type_id:
            params["appointment_type_id"] = appointment_type_id
        if slot_length:
            params["slot_length"] = slot_length
        
        # Set up headers with bearer token
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {bearer_token}",
            "Nex-Api-Version": "v20240412"
        }
        
        print(f"[SLOTS] Checking availability: {start_date} for {days} days, params: {params}")
        
        # Make API request
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SYNCRONIZER_BASE_URL}/available_slots",
                params=params,
                headers=headers,
                timeout=15.0  # Longer timeout for slot searches
            )
            
            if response.status_code == 200:
                data = response.json()
                slots = data.get("data", [])
                next_available_date = data.get("next_available_date")
                
                if not slots:
                    message = f"No available slots found for the requested dates ({start_date} to {days} days)."
                    if next_available_date:
                        message += f" The next available appointment is {next_available_date}."
                    
                    return {
                        "success": False,
                        "message": message,
                        "slots": [],
                        "next_available_date": next_available_date
                    }
                
                # Format slot results for voice agent
                formatted_slots = []
                
                # The API returns data like: [{"lid": 334724, "pid": 426683283, "slots": [...]}]
                # We need to extract the actual slots from each provider group
                print(f"[SLOTS DEBUG] Processing {len(slots)} provider groups")
                for i, provider_slot_group in enumerate(slots):
                    provider_id = provider_slot_group.get("pid")
                    location_id = provider_slot_group.get("lid") 
                    actual_slots = provider_slot_group.get("slots", [])
                    print(f"[SLOTS DEBUG] Group {i}: Provider {provider_id}, {len(actual_slots)} slots")
                    
                    # Get provider info for this group
                    provider_info = {}
                    if provider_ids and len(provider_ids) == 1:
                        # Single provider request - we can get provider details
                        providers_result = await get_providers(location_id=location_id)
                        if providers_result["success"]:
                            matching_provider = next((p for p in providers_result["providers"] if p["id"] == provider_id), None)
                            if matching_provider:
                                provider_info = matching_provider
                    
                    # Process each actual appointment slot
                    for j, slot in enumerate(actual_slots[:10]):  # Limit to 10 slots per provider for voice interaction
                        # Parse the slot data
                        slot_time = slot.get("time") or slot.get("start_time")
                        if j < 3:  # Debug first 3 slots
                            print(f"[SLOTS DEBUG]   Slot {j}: {slot_time} | Raw: {slot}")
                        
                        # Format date and time for natural speech
                        if slot_time:
                            try:
                                from datetime import datetime
                                # Parse ISO format datetime
                                dt = datetime.fromisoformat(slot_time.replace('Z', '+00:00'))
                                # Format for voice: "Tuesday, December 3rd at 2:30 PM"
                                formatted_date = dt.strftime("%A, %B %d")
                                # Add ordinal suffix to day
                                day = dt.day
                                if 10 <= day % 100 <= 20:
                                    suffix = "th"
                                else:
                                    suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
                                formatted_date = formatted_date.replace(f" {day}", f" {day}{suffix}")
                                
                                formatted_time = dt.strftime("%I:%M %p").lstrip('0')
                                friendly_datetime = f"{formatted_date} at {formatted_time}"
                                if j < 3:  # Debug formatting
                                    print(f"[SLOTS DEBUG]     Formatted: {friendly_datetime}")
                            except Exception as e:
                                # Fallback to raw time if parsing fails
                                friendly_datetime = slot_time
                                print(f"[SLOTS DEBUG]     Parse error: {e}")
                        else:
                            friendly_datetime = "Time not available"
                        
                        formatted_slot = {
                            "start_time": slot_time,
                            "friendly_datetime": friendly_datetime,
                            "duration_minutes": slot.get("duration_minutes", slot.get("duration", 30)),
                            "provider_id": provider_info.get("id") if isinstance(provider_info, dict) else slot.get("provider_id"),
                            "provider_name": provider_info.get("name") if isinstance(provider_info, dict) else "Available Provider",
                            "location_id": slot.get("location_id", params.get("lids[]", [SYNCRONIZER_LOCATION_ID])[0] if params.get("lids[]") else SYNCRONIZER_LOCATION_ID),
                            "slot_id": slot.get("id"),
                            "operatory_id": slot.get("operatory_id")
                        }
                        formatted_slots.append(formatted_slot)
                        
                        # Break if we have enough slots for voice interaction
                        if len(formatted_slots) >= 10:
                            break
                
                # Calculate total slots across all providers
                total_slots = sum(len(group.get("slots", [])) for group in slots)
                
                print(f"[SLOTS FINAL] Formatted {len(formatted_slots)} slots out of {total_slots} total")
                if formatted_slots:
                    print(f"[SLOTS FINAL] Sample times: {formatted_slots[0]['friendly_datetime']}")
                    if len(formatted_slots) > 1:
                        print(f"[SLOTS FINAL]              {formatted_slots[1]['friendly_datetime']}")
                    if len(formatted_slots) > 2:
                        print(f"[SLOTS FINAL]              {formatted_slots[2]['friendly_datetime']}")
                
                return {
                    "success": True,
                    "message": f"Found {len(formatted_slots)} available appointment slots (showing first 10 of {total_slots} total).",
                    "slots": formatted_slots,
                    "total_count": total_slots,
                    "displayed_count": len(formatted_slots),
                    "next_available_date": next_available_date
                }
            
            else:
                return {
                    "success": False,
                    "message": f"API error while checking availability: {response.status_code} - {response.text}",
                    "slots": []
                }
                
    except httpx.TimeoutException:
        return {
            "success": False,
            "message": "Request timed out while checking availability. Please try again.",
            "slots": []
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Error checking availability: {str(e)}",
            "slots": []
        }

async def log_and_execute_tool(
    chat_id: str,
    tool_call_message: ToolCallMessage,
    handler_func,
    control_plane_client: AsyncControlPlaneClient
):
    """
    Wrapper to log tool calls and their results to Supabase.
    
    Args:
        chat_id: Chat ID
        tool_call_message: Tool call message from Hume
        handler_func: The actual handler function to execute
        control_plane_client: Control plane client
    """
    start_time = time.time()
    tool_call_id = tool_call_message.tool_call_id
    tool_name = tool_call_message.name
    
    # Set context variables for API logging
    _current_chat_id.set(chat_id)
    _current_tool_call_id.set(tool_call_id)
    
    # Parse parameters
    parameters_str = tool_call_message.parameters or "{}"
    if isinstance(parameters_str, str):
        try:
            parameters = json.loads(parameters_str)
        except:
            parameters = {"raw": parameters_str}
    else:
        parameters = parameters_str or {}
    
    # Log tool call start
    await log_tool_call_event(
        chat_id=chat_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_type=getattr(tool_call_message, 'tool_type', 'function'),
        parameters=parameters,
        response_required=getattr(tool_call_message, 'response_required', True),
        webhook_payload=tool_call_message.dict() if hasattr(tool_call_message, 'dict') else {}
    )
    
    # Execute the handler
    try:
        result = await handler_func(control_plane_client, chat_id, tool_call_message)
        execution_time_ms = int((time.time() - start_time) * 1000)
        
        # Log success
        await log_tool_call_result(
            tool_call_id=tool_call_id,
            success=True,
            result_summary=f"{tool_name} executed successfully",
            execution_time_ms=execution_time_ms,
            response_type="tool_response"
        )
        
        return result
    except Exception as e:
        execution_time_ms = int((time.time() - start_time) * 1000)
        
        # Log error
        await log_tool_call_result(
            tool_call_id=tool_call_id,
            success=False,
            error_type=type(e).__name__,
            error_message=str(e),
            execution_time_ms=execution_time_ms,
            response_type="tool_error"
        )
        
        raise
    finally:
        # Clear context variables
        _current_chat_id.set(None)
        _current_tool_call_id.set(None)

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
                print(f"[SEARCH] Found {len(result['patients'])} patient(s)")
                # Format patient list for natural speech INCLUDING patient ID
                patient_list = []
                for patient in result["patients"]:
                    # CRITICAL: Include patient ID so AI can use it for booking
                    patient_id = patient.get('id', 'UNKNOWN')
                    patient_name = patient.get('name', 'Unknown Name')
                    
                    print(f"[SEARCH] Processing patient - ID: {patient_id}, Name: {patient_name}")
                    
                    if patient_id == 'UNKNOWN' or patient_id is None:
                        print(f"[SEARCH WARNING] Patient has no ID! Full patient data: {patient}")
                    
                    patient_info = f"{patient_name} (Patient ID: {patient_id}"
                    if patient.get('phone'):
                        patient_info += f", phone: {patient['phone']}"
                    if patient.get('date_of_birth'):
                        patient_info += f", DOB: {patient['date_of_birth']}"
                    patient_info += ")"
                    patient_list.append(patient_info)
                    print(f"[SEARCH] Formatted: {patient_info}")
                
                if len(patient_list) == 1:
                    # Single patient found - be VERY explicit about the patient ID
                    patient = result["patients"][0]
                    patient_id = patient.get('id', 'UNKNOWN')
                    response_content = f"I found 1 patient: {patient_list[0]}. "
                    response_content += f"The patient ID is {patient_id}. "
                    response_content += f"Please use this patient ID {patient_id} when booking an appointment. "
                    response_content += f"Is this the correct patient for booking?"
                else:
                    # Multiple patients - list them with explicit IDs
                    response_content = f"I found {len(patient_list)} patients:\n"
                    for i, (patient_info, patient) in enumerate(zip(patient_list, result["patients"]), 1):
                        patient_id = patient.get('id', 'UNKNOWN')
                        response_content += f"{i}. {patient_info} - Use Patient ID: {patient_id} for booking\n"
                    response_content += "Which patient would you like to select?"
            else:
                print(f"[SEARCH] No patients found matching the search criteria")
                response_content = "I couldn't find any patients matching your search. Could you please verify the spelling of the name, or try providing a phone number or date of birth?"
        else:
            response_content = f"I encountered an issue while searching for patients: {result['message']}"
        
        # Send the result as a tool response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolResponseMessage(
                tool_call_id=tool_call_id,
                content=response_content
            )
        )
        print(f"[SUCCESS] Patient search completed successfully!")
        
    except Exception as e:
        print(f"[ERROR] Failed to handle search patients tool: {e}")
        
        # Send error response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolErrorMessage(
                tool_call_id=tool_call_id,
                error="PatientSearchError",
                content=f"I'm having trouble searching for patients right now. Please try again or contact our office directly. Error: {str(e)}"
            )
            )

async def handle_create_patient_tool(control_plane_client: AsyncControlPlaneClient, chat_id: str, tool_call_message: ToolCallMessage):
    """
    Handle the create_patient tool call and send the response back to the chat.
    
    Args:
        control_plane_client: The control plane client instance
        chat_id: The ID of the chat
        tool_call_message: The tool call message
    """
    tool_call_id = tool_call_message.tool_call_id
    tool_name = tool_call_message.name
    
    print(f"[TOOL] Processing tool: {tool_name}")
    print(f"[TOOL] Tool call ID: {tool_call_id}")
    
    if tool_name != "create_patient":
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
        
        # Extract required parameters
        first_name = parameters.get("first_name")
        last_name = parameters.get("last_name")
        date_of_birth = parameters.get("date_of_birth")
        email = parameters.get("email")
        phone_number = parameters.get("phone_number")
        
        # Extract optional parameters
        middle_name = parameters.get("middle_name")
        
        # Handle address if provided
        address = parameters.get("address")
        if address and isinstance(address, str):
            # If address is a string, try to parse it as JSON
            try:
                address = json.loads(address)
            except:
                # If parsing fails, create a simple dict with street_address
                address = {"street_address": address}
        
        print(f"[CREATE] Creating patient: {first_name} {last_name}, DOB: {date_of_birth}")
        
        # Validate required fields
        if not all([first_name, last_name, date_of_birth, email, phone_number]):
            missing_fields = []
            if not first_name:
                missing_fields.append("first name")
            if not last_name:
                missing_fields.append("last name")
            if not date_of_birth:
                missing_fields.append("date of birth")
            if not email:
                missing_fields.append("email")
            if not phone_number:
                missing_fields.append("phone number")
            
            error_msg = f"I need the following information to create a patient record: {', '.join(missing_fields)}. Could you please provide that?"
            await safe_send_to_control_plane(
                control_plane_client,
                chat_id,
                ToolResponseMessage(
                    tool_call_id=tool_call_id,
                    content=error_msg
                )
            )
            return
        
        # Create the patient
        result = await create_patient(
            first_name=first_name,
            last_name=last_name,
            date_of_birth=date_of_birth,
            email=email,
            phone_number=phone_number,
            middle_name=middle_name,
            address=address
        )
        
        # Format response for voice agent
        if result["success"]:
            patient = result["patient"]
            response_content = f"Great! I've successfully created a patient record for {patient['name']}"
            
            # Add confirmation details
            details = []
            if patient.get('date_of_birth'):
                details.append(f"date of birth {patient['date_of_birth']}")
            if patient.get('phone'):
                details.append(f"phone number {patient['phone']}")
            if patient.get('email'):
                details.append(f"email {patient['email']}")
            
            if details:
                response_content += f" with {', '.join(details)}"
            
            response_content += f". The patient ID is {patient['id']}. Would you like to schedule an appointment for {patient['name']}?"
        else:
            response_content = f"I encountered an issue while creating the patient record: {result['message']}. Please try again or contact our office for assistance."
        
        # Send the result as a tool response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolResponseMessage(
                tool_call_id=tool_call_id,
                content=response_content
            )
        )
        print(f"[SUCCESS] Patient creation completed successfully!")
        
    except Exception as e:
        print(f"[ERROR] Failed to handle create patient tool: {e}")
        
        # Send error response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolErrorMessage(
                tool_call_id=tool_call_id,
                error="PatientCreationError",
                content=f"I'm having trouble creating the patient record right now. Please try again or contact our office directly. Error: {str(e)}"
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
                # Format provider list for natural speech WITH IDs for booking
                providers = result["providers"]
                
                if len(providers) == 1:
                    provider = providers[0]
                    response_content = f"I found {provider['name']}."
                    if provider.get('speciality'):
                        response_content += f" They specialize in {provider['speciality']}."
                    response_content += f" Their provider ID is {provider['id']}. Would you like to check their availability?"
                    
                elif len(providers) <= 5:
                    response_content = f"I found {len(providers)} providers:\n"
                    for provider in providers:
                        provider_info = f"• {provider['name']} (ID: {provider['id']})"
                        if provider.get('speciality'):
                            provider_info += f" - {provider['speciality']}"
                        if not provider.get('requestable', True):
                            provider_info += " - Not available for online booking"
                        response_content += f"{provider_info}\n"
                    response_content += "To check availability for a specific doctor, use their provider ID when requesting appointment slots."
                    
                else:
                    # Show first 5 if many results
                    response_content = f"I found {len(providers)} providers. Here are the first 5:\n"
                    for provider in providers[:5]:
                        provider_info = f"• {provider['name']} (ID: {provider['id']})"
                        if provider.get('speciality'):
                            provider_info += f" - {provider['speciality']}"
                        response_content += f"{provider_info}\n"
                    response_content += "To check availability, use the provider ID. Would you like to see more doctors or check availability for one of these?"
            else:
                if provider_name:
                    response_content = f"I couldn't find a provider named '{provider_name}'. Could you check the spelling or try a different name? I can also show you all available providers."
                else:
                    response_content = "I couldn't find any providers matching your criteria. Let me check our available doctors for you."
        else:
            response_content = f"I encountered an issue while looking up providers: {result['message']}"
        
        # Send the result as a tool response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolResponseMessage(
                tool_call_id=tool_call_id,
                content=response_content
            )
        )
        print(f"[SUCCESS] Provider search completed successfully!")
        
    except Exception as e:
        print(f"[ERROR] Failed to handle get providers tool: {e}")
        
        # Send error response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolErrorMessage(
                tool_call_id=tool_call_id,
                error="ProviderSearchError",
                content=f"I'm having trouble finding provider information right now. Please try again or contact our office directly. Error: {str(e)}"
            )
            )

async def handle_get_available_slots_tool(control_plane_client: AsyncControlPlaneClient, chat_id: str, tool_call_message: ToolCallMessage):
    """
    Handle the get_available_slots tool call and send the response back to the chat.
    
    Args:
        control_plane_client: The control plane client instance
        chat_id: The ID of the chat
        tool_call_message: The tool call message
    """
    tool_call_id = tool_call_message.tool_call_id
    tool_name = tool_call_message.name
    
    print(f"[TOOL] Processing tool: {tool_name}")
    print(f"[TOOL] Tool call ID: {tool_call_id}")
    
    if tool_name != "get_available_slots":
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
        
        # Extract required parameters
        start_date = parameters.get("start_date")
        days = parameters.get("days", 7)  # Default to 7 days if not specified
        
        # Extract optional parameters
        provider_ids = parameters.get("provider_ids")
        location_ids = parameters.get("location_ids") 
        appointment_type_id = parameters.get("appointment_type_id")
        slot_length = parameters.get("slot_length")
        
        # Validate required parameters
        if not start_date:
            # Default to today if no start date provided
            from datetime import date
            start_date = date.today().isoformat()
        
        print(f"[SLOTS] Checking availability: start_date={start_date}, days={days}, providers={provider_ids}, appointment_type={appointment_type_id}")
        
        # Get available slots
        result = await get_available_slots(
            start_date=start_date,
            days=days,
            provider_ids=provider_ids,
            location_ids=location_ids,
            appointment_type_id=appointment_type_id,
            slot_length=slot_length
        )
        
        # Format response for voice agent
        if result["success"]:
            if result["slots"]:
                slots = result["slots"]
                print(f"[HANDLER] Formatting {len(slots)} slots for AI response")
                
                if len(slots) == 1:
                    slot = slots[0]
                    response_content = f"I found 1 available appointment: {slot['friendly_datetime']}"
                    if slot.get('provider_name') and slot['provider_name'] != "Available Provider":
                        response_content += f" with {slot['provider_name']}"
                    response_content += ". Would you like to book this appointment?"
                    
                elif len(slots) <= 5:
                    response_content = f"I found {len(slots)} available appointments:\n"
                    for i, slot in enumerate(slots, 1):
                        slot_info = f"{i}. {slot['friendly_datetime']}"
                        if slot.get('provider_name') and slot['provider_name'] != "Available Provider":
                            slot_info += f" with {slot['provider_name']}"
                        response_content += f"{slot_info}\n"
                    response_content += "Which appointment time works best for you?"
                    print(f"[HANDLER] Sending {len(slots)} slots to AI")
                    
                else:
                    # Show first 5 if many results
                    response_content = f"I found {len(slots)} available appointments. Here are the next 5 options:\n"
                    for i, slot in enumerate(slots[:5], 1):
                        slot_info = f"{i}. {slot['friendly_datetime']}"
                        if slot.get('provider_name') and slot['provider_name'] != "Available Provider":
                            slot_info += f" with {slot['provider_name']}"
                        response_content += f"{slot_info}\n"
                    response_content += "Which time works for you, or would you like to see more options?"
                    print(f"[HANDLER] Sending first 5 of {len(slots)} total slots to AI")
                    
            else:
                # No slots available
                response_content = result["message"]
                
                # Suggest alternatives if next_available_date is provided
                if result.get("next_available_date"):
                    response_content += f" Would you like to check availability starting {result['next_available_date']}?"
                else:
                    response_content += " Would you like to try different dates or times?"
        else:
            response_content = f"I encountered an issue while checking availability: {result['message']}"
        
        # Send the result as a tool response
        print(f"[HANDLER] Sending response to AI: {response_content[:200]}...")
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolResponseMessage(
                tool_call_id=tool_call_id,
                content=response_content
            )
        )
        print(f"[SUCCESS] Available slots search completed successfully!")
        
    except Exception as e:
        print(f"[ERROR] Failed to handle get available slots tool: {e}")
        
        # Send error response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolErrorMessage(
                tool_call_id=tool_call_id,
                error="AvailabilitySearchError",
                content=f"I'm having trouble checking appointment availability right now. Please try again or call our office directly. Error: {str(e)}"
            )
            )

async def handle_get_locations_tool(control_plane_client: AsyncControlPlaneClient, chat_id: str, tool_call_message: ToolCallMessage):
    """
    Handle the get_locations tool call and send the response back to the chat.
    
    Args:
        control_plane_client: The control plane client instance
        chat_id: The ID of the chat
        tool_call_message: The tool call message
    """
    tool_call_id = tool_call_message.tool_call_id
    tool_name = tool_call_message.name
    
    print(f"[TOOL] Processing tool: {tool_name}")
    print(f"[TOOL] Tool call ID: {tool_call_id}")
    
    if tool_name != "get_locations":
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
        location_name = parameters.get("location_name")
        include_inactive = parameters.get("include_inactive", False)
        
        print(f"[LOCATIONS] Searching locations with: location_name={location_name}, include_inactive={include_inactive}")
        
        # Get locations
        result = await get_locations(
            location_name=location_name,
            include_inactive=include_inactive
        )
        
        # Format response for voice agent
        if result["success"]:
            if result["locations"]:
                locations = result["locations"]
                
                if len(locations) == 1:
                    location = locations[0]
                    
                    # Build address string
                    address_parts = []
                    if location.get('address'):
                        address_parts.append(location['address'])
                    if location.get('city'):
                        address_parts.append(location['city'])
                    if location.get('state'):
                        address_parts.append(location['state'])
                    
                    full_address = ", ".join(address_parts) if address_parts else "Address available upon request"
                    
                    response_content = f"We're located at {location['name']} at {full_address}."
                    if location.get('phone'):
                        response_content += f" Our phone number is {location['phone']}."
                    response_content += " Would you like to schedule an appointment at this location?"
                    
                else:
                    # Multiple locations (future expansion)
                    response_content = f"We have {len(locations)} locations:\n"
                    for i, location in enumerate(locations, 1):
                        location_info = f"{i}. {location['name']}"
                        if location.get('city'):
                            location_info += f" in {location['city']}"
                        if location.get('inactive'):
                            location_info += " (currently closed)"
                        response_content += f"{location_info}\n"
                    response_content += "Which location would you prefer for your appointment?"
                    
            else:
                response_content = "I'm having trouble finding our location information. Let me connect you with someone who can help with scheduling."
        else:
            response_content = f"I encountered an issue while looking up our location: {result['message']}"
        
        # Send the result as a tool response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolResponseMessage(
                tool_call_id=tool_call_id,
                content=response_content
            )
        )
        print(f"[SUCCESS] Location search completed successfully!")
        
    except Exception as e:
        print(f"[ERROR] Failed to handle get locations tool: {e}")
        
        # Send error response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolErrorMessage(
                tool_call_id=tool_call_id,
                error="LocationSearchError",
                content=f"I'm having trouble finding location information right now. Please try again or contact our office directly. Error: {str(e)}"
            )
        )

async def handle_book_appointment_tool(control_plane_client: AsyncControlPlaneClient, chat_id: str, tool_call_message: ToolCallMessage):
    """
    Handle the book_appointment tool call and send the response back to the chat.
    
    Args:
        control_plane_client: The control plane client instance
        chat_id: The ID of the chat
        tool_call_message: The tool call message
    """
    tool_call_id = tool_call_message.tool_call_id
    tool_name = tool_call_message.name
    
    print(f"[TOOL] Processing tool: {tool_name}")
    print(f"[TOOL] Tool call ID: {tool_call_id}")
    
    if tool_name != "book_appointment":
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
        
        # Extract required parameters
        patient_id = parameters.get("patient_id")
        provider_id = parameters.get("provider_id")
        start_time = parameters.get("start_time")
        
        # Extract optional parameters
        end_time = parameters.get("end_time")
        appointment_type_id = parameters.get("appointment_type_id")
        operatory_id = parameters.get("operatory_id")
        note = parameters.get("note")
        notify_patient = parameters.get("notify_patient", True)
        
        print(f"[BOOK APPOINTMENT] Patient: {patient_id}, Provider: {provider_id}, Start: {start_time}")
        
        # Validate required fields
        if not all([patient_id, provider_id, start_time]):
            missing_fields = []
            if not patient_id:
                missing_fields.append("patient ID")
            if not provider_id:
                missing_fields.append("provider ID")
            if not start_time:
                missing_fields.append("start time")
            
            error_msg = f"I need the following information to book the appointment: {', '.join(missing_fields)}. Could you please provide that?"
            await safe_send_to_control_plane(
                control_plane_client,
                chat_id,
                ToolResponseMessage(
                    tool_call_id=tool_call_id,
                    content=error_msg
                )
            )
            return
        
        # Book the appointment
        result = await book_appointment(
            patient_id=patient_id,
            provider_id=provider_id,
            start_time=start_time,
            end_time=end_time,
            appointment_type_id=appointment_type_id,
            operatory_id=operatory_id,
            note=note,
            notify_patient=notify_patient
        )
        
        # Format response for voice agent
        if result["success"]:
            appointment = result["appointment"]
            
            # Parse and format the start time for voice (convert to local timezone)
            from datetime import datetime
            from zoneinfo import ZoneInfo
            try:
                dt_utc = datetime.fromisoformat(appointment['start_time'].replace('Z', '+00:00'))
                # Convert to appointment's local timezone
                appt_timezone = appointment.get('timezone', 'America/New_York')
                dt_local = dt_utc.astimezone(ZoneInfo(appt_timezone))
                formatted_time = dt_local.strftime("%A, %B %d at %I:%M %p")
            except:
                formatted_time = appointment['start_time']
            
            response_content = f"Great! I've booked your appointment with {appointment['provider_name']} for {formatted_time}."
            
            if appointment.get('note'):
                response_content += f" Note: {appointment['note']}"
            
            response_content += " You should receive a confirmation shortly. Is there anything else I can help you with?"
            
            # Add to outbound calls queue for reminder
            try:
                if supabase_client:
                    # Get patient phone number
                    patient_data = await get_patient_by_id(patient_id)
                    if patient_data and patient_data.get("phone_number"):
                        # Parse appointment time and timezone
                        appt_time = appointment.get('start_time')
                        appt_timezone = appointment.get('timezone', 'America/New_York')
                        
                        # Insert into outbound_calls table
                        supabase_client.table("outbound_calls").insert({
                            "patient_id": str(patient_id),
                            "appointment_id": str(appointment.get('id')),
                            "phone_number": patient_data["phone_number"],
                            "appointment_time": appt_time,
                            "timezone": appt_timezone,
                            "provider_id": str(provider_id) if provider_id else None,
                            "status": "pending"
                        }).execute()
                        print(f"[OUTBOUND] Added reminder call for appointment {appointment.get('id')} with provider {provider_id}")
                    else:
                        print(f"[OUTBOUND] No phone number found for patient {patient_id}, skipping reminder")
            except Exception as outbound_err:
                # Don't fail the booking if outbound call insert fails
                print(f"[OUTBOUND ERROR] Failed to add reminder call: {outbound_err}")
        else:
            # Check if the error is related to invalid patient ID
            error_detail = result.get('error_detail', '')
            if 'Patient with id' in error_detail and 'not found' in error_detail:
                response_content = f"I'm sorry, I couldn't find that patient record. Please search for the patient first using their name, phone number, or date of birth before booking an appointment."
            else:
                # When booking fails, automatically check for existing appointments
                print(f"[BOOK APPOINTMENT] Booking failed, checking existing appointments for patient {patient_id}")
                existing_appts = await get_patient_appointments(patient_id=patient_id)
                
                if existing_appts["success"] and existing_appts["appointments"]:
                    # Found existing appointments - inform the user
                    from datetime import datetime
                    from zoneinfo import ZoneInfo
                    
                    appointments = existing_appts["appointments"]
                    response_content = f"I'm sorry, that time slot is no longer available. "
                    
                    if len(appointments) == 1:
                        appt = appointments[0]
                        try:
                            dt_utc = datetime.fromisoformat(appt['start_time'].replace('Z', '+00:00'))
                            appt_timezone = appt.get('timezone', 'America/New_York')
                            dt_local = dt_utc.astimezone(ZoneInfo(appt_timezone))
                            formatted_time = dt_local.strftime("%A, %B %d at %I:%M %p %Z")
                        except:
                            formatted_time = appt['start_time']
                        
                        response_content += f"However, I see you already have an appointment scheduled for {formatted_time} with {appt['provider_name']}. "
                        response_content += "Would you like to keep that appointment, reschedule it, or book an additional appointment?"
                    else:
                        response_content += f"However, I see you have {len(appointments)} appointments already scheduled. "
                        response_content += "Would you like me to review your existing appointments, or try booking a different time?"
                else:
                    # No existing appointments found
                    response_content = f"I'm sorry, I had trouble booking that appointment. {result['message']} Would you like to try a different time or provider?"
        
        # Send the result as a tool response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolResponseMessage(
                tool_call_id=tool_call_id,
                content=response_content
            )
        )
        print(f"[SUCCESS] Appointment booking completed successfully!")
        
    except Exception as e:
        print(f"[ERROR] Failed to handle book appointment tool: {e}")
        
        # Send error response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolErrorMessage(
                tool_call_id=tool_call_id,
                error="AppointmentBookingError",
                content=f"I'm having trouble booking the appointment right now. Please try again or contact our office directly at our main number. Error: {str(e)}"
            )
        )

async def handle_get_patient_appointments_tool(control_plane_client: AsyncControlPlaneClient, chat_id: str, tool_call_message: ToolCallMessage):
    """
    Handle the get_patient_appointments tool call and send the response back to the chat.
    
    Args:
        control_plane_client: The control plane client instance
        chat_id: The ID of the chat
        tool_call_message: The tool call message
    """
    tool_call_id = tool_call_message.tool_call_id
    tool_name = tool_call_message.name
    
    print(f"[TOOL] Processing tool: {tool_name}")
    print(f"[TOOL] Tool call ID: {tool_call_id}")
    
    if tool_name != "get_patient_appointments":
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
        
        # Extract parameters
        patient_id = parameters.get("patient_id")
        start_date = parameters.get("start_date")
        end_date = parameters.get("end_date")
        include_cancelled = parameters.get("include_cancelled", False)
        
        print(f"[APPOINTMENTS] Patient: {patient_id}, Start: {start_date}, End: {end_date}")
        
        # Validate required fields
        if not patient_id:
            error_msg = "I need a patient ID to check appointments. Please search for the patient first."
            await safe_send_to_control_plane(
                control_plane_client,
                chat_id,
                ToolResponseMessage(
                    tool_call_id=tool_call_id,
                    content=error_msg
                )
            )
            return
        
        # Get appointments
        result = await get_patient_appointments(
            patient_id=patient_id,
            start_date=start_date,
            end_date=end_date,
            cancelled=include_cancelled
        )
        
        # Format response for voice agent
        if result["success"]:
            appointments = result["appointments"]
            
            if appointments:
                # Parse and format appointment times
                from datetime import datetime
                
                response_content = f"I found {len(appointments)} appointment(s):\n\n"
                
                for i, appt in enumerate(appointments, 1):
                    try:
                        # Parse ISO datetime (UTC) and convert to appointment's timezone
                        from zoneinfo import ZoneInfo
                        
                        # Parse UTC time
                        dt_utc = datetime.fromisoformat(appt['start_time'].replace('Z', '+00:00'))
                        
                        # Convert to appointment's timezone
                        appt_timezone = appt.get('timezone', 'America/New_York')
                        dt_local = dt_utc.astimezone(ZoneInfo(appt_timezone))
                        
                        # Format in local time
                        formatted_time = dt_local.strftime("%A, %B %d at %I:%M %p %Z")
                    except Exception as e:
                        print(f"[APPOINTMENTS WARNING] Failed to parse time: {e}")
                        formatted_time = appt['start_time']
                    
                    status = "Cancelled" if appt.get('cancelled') else ("Confirmed" if appt.get('confirmed') else "Pending")
                    
                    response_content += f"{i}. {formatted_time} with {appt['provider_name']} - Status: {status}"
                    
                    if appt.get('note'):
                        response_content += f" (Note: {appt['note']})"
                    
                    response_content += "\n"
                
                response_content += "\nWould you like to reschedule any of these appointments, or book a new one?"
            else:
                response_content = "You don't have any upcoming appointments scheduled. Would you like to book one?"
        else:
            response_content = f"I had trouble checking your appointments: {result['message']}. Let me try to help you in another way."
        
        # Send the result as a tool response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolResponseMessage(
                tool_call_id=tool_call_id,
                content=response_content
            )
        )
        print(f"[SUCCESS] Appointment check completed successfully!")
        
    except Exception as e:
        print(f"[ERROR] Failed to handle get patient appointments tool: {e}")
        
        # Send error response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolErrorMessage(
                tool_call_id=tool_call_id,
                error="AppointmentCheckError",
                content=f"I'm having trouble checking appointments right now. Please try again or contact our office directly. Error: {str(e)}"
            )
        )

async def handle_reschedule_appointment_tool(control_plane_client: AsyncControlPlaneClient, chat_id: str, tool_call_message: ToolCallMessage):
    """
    Handle the reschedule_appointment tool call and send the response back to the chat.
    
    Args:
        control_plane_client: The control plane client instance
        chat_id: The ID of the chat
        tool_call_message: The tool call message
    """
    tool_call_id = tool_call_message.tool_call_id
    tool_name = tool_call_message.name
    
    print(f"[TOOL] Processing tool: {tool_name}")
    print(f"[TOOL] Tool call ID: {tool_call_id}")
    
    if tool_name != "reschedule_appointment":
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
        
        # Extract parameters
        appointment_id = parameters.get("appointment_id")
        start_time = parameters.get("start_time")
        end_time = parameters.get("end_time")
        provider_id = parameters.get("provider_id")
        operatory_id = parameters.get("operatory_id")
        note = parameters.get("note")
        cancelled = parameters.get("cancelled", False)
        confirmed = parameters.get("confirmed")
        notify_patient = parameters.get("notify_patient", True)
        
        print(f"[RESCHEDULE] Appointment ID: {appointment_id}, New Start: {start_time}, Cancelled: {cancelled}")
        
        # Validate required fields
        if not appointment_id:
            error_msg = "I need an appointment ID to reschedule. Please provide the appointment ID."
            await safe_send_to_control_plane(
                control_plane_client,
                chat_id,
                ToolResponseMessage(
                    tool_call_id=tool_call_id,
                    content=error_msg
                )
            )
            return
        
        # Reschedule the appointment
        result = await reschedule_appointment(
            appointment_id=appointment_id,
            start_time=start_time,
            end_time=end_time,
            provider_id=provider_id,
            operatory_id=operatory_id,
            note=note,
            cancelled=cancelled,
            confirmed=confirmed,
            notify_patient=notify_patient
        )
        
        # Format response for voice agent
        if result["success"]:
            appointment = result["appointment"]
            
            # Determine what action was performed
            if cancelled:
                response_content = f"I've cancelled the appointment successfully."
                if appointment.get('provider_name'):
                    response_content += f" Your appointment with {appointment['provider_name']} has been cancelled."
                response_content += " Is there anything else I can help you with?"
                
                # Update outbound_calls to cancelled
                try:
                    if supabase_client:
                        supabase_client.table("outbound_calls").update({
                            "status": "cancelled",
                            "updated_at": "now()"
                        }).eq("appointment_id", str(appointment_id)).execute()
                        print(f"[OUTBOUND] Cancelled reminder call for appointment {appointment_id}")
                except Exception as outbound_err:
                    print(f"[OUTBOUND ERROR] Failed to cancel reminder: {outbound_err}")
            else:
                # Parse and format the start time for voice
                from datetime import datetime
                from zoneinfo import ZoneInfo
                
                try:
                    dt_utc = datetime.fromisoformat(appointment['start_time'].replace('Z', '+00:00'))
                    appt_timezone = appointment.get('timezone', 'America/New_York')
                    dt_local = dt_utc.astimezone(ZoneInfo(appt_timezone))
                    formatted_time = dt_local.strftime("%A, %B %d at %I:%M %p %Z")
                except:
                    formatted_time = appointment['start_time']
                
                provider_name = appointment.get('provider_name', 'your provider')
                
                response_content = f"Perfect! I've rescheduled your appointment to {formatted_time} with {provider_name}."
                
                if appointment.get('note'):
                    response_content += f" Note: {appointment['note']}"
                
                response_content += " You should receive a confirmation shortly. Is there anything else I can help you with?"
                
                # Update outbound_calls with new appointment time
                try:
                    if supabase_client:
                        supabase_client.table("outbound_calls").update({
                            "appointment_time": appointment.get('start_time'),
                            "status": "pending",  # Reset to pending for new reminder
                            "updated_at": "now()"
                        }).eq("appointment_id", str(appointment_id)).execute()
                        print(f"[OUTBOUND] Updated reminder call for appointment {appointment_id}")
                except Exception as outbound_err:
                    print(f"[OUTBOUND ERROR] Failed to update reminder: {outbound_err}")
        else:
            # Handle different error scenarios
            error_detail = result.get('error_detail', '')
            
            if 'not found' in error_detail.lower():
                response_content = "I'm sorry, I couldn't find that appointment. Could you provide the appointment ID again or check your appointments?"
            elif 'not available' in error_detail.lower():
                response_content = f"I'm sorry, that time slot is no longer available. Would you like to try a different time?"
            else:
                response_content = f"I'm sorry, I had trouble rescheduling that appointment. {result['message']} Would you like to try again or choose a different option?"
        
        # Send the result as a tool response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolResponseMessage(
                tool_call_id=tool_call_id,
                content=response_content
            )
        )
        print(f"[SUCCESS] Appointment reschedule completed successfully!")
        
    except Exception as e:
        print(f"[ERROR] Failed to handle reschedule appointment tool: {e}")
        
        # Send error response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolErrorMessage(
                tool_call_id=tool_call_id,
                error="AppointmentRescheduleError",
                content=f"I'm having trouble rescheduling the appointment right now. Please try again or contact our office directly. Error: {str(e)}"
            )
        )

async def handle_forward_call_tool(control_plane_client: AsyncControlPlaneClient, chat_id: str, tool_call_message: ToolCallMessage):
    """
    Handle the forward_call tool - transfers the current call to a staff member (cold transfer).
    
    This uses Twilio's call update API to redirect the active call to our TwiML endpoint,
    which then dials the forward number.
    
    Args:
        control_plane_client: The control plane client instance
        chat_id: The ID of the chat
        tool_call_message: The tool call message
    """
    tool_call_id = tool_call_message.tool_call_id
    tool_name = tool_call_message.name
    
    print(f"[TOOL] Processing tool: {tool_name}")
    print(f"[TOOL] Tool call ID: {tool_call_id}")
    
    if tool_name != "forward_call":
        print(f"[ERROR] Unknown tool: {tool_name}")
        return
    
    try:
        # Parse parameters from tool call
        parameters = {}
        if hasattr(tool_call_message, 'parameters') and tool_call_message.parameters:
            if isinstance(tool_call_message.parameters, str):
                try:
                    parameters = json.loads(tool_call_message.parameters)
                except:
                    parameters = {}
            else:
                parameters = tool_call_message.parameters
        
        # Get optional reason for the transfer
        reason = parameters.get("reason", "Patient requested to speak with staff")
        forward_to = parameters.get("forward_to", CALL_FORWARD_NUMBER)
        
        print(f"[FORWARD CALL] Attempting to transfer call")
        print(f"[FORWARD CALL] Reason: {reason}")
        print(f"[FORWARD CALL] Forward to: {forward_to}")
        
        # Check if Twilio client is available
        if not twilio_client:
            response_content = "I apologize, but I'm unable to transfer the call right now. Our call transfer service is temporarily unavailable. Please call our office directly at the number provided, or I can help you with your request."
            await safe_send_to_control_plane(
                control_plane_client,
                chat_id,
                ToolResponseMessage(
                    tool_call_id=tool_call_id,
                    content=response_content
                )
            )
            return
        
        # Get the active call SID from the chat
        # We need to find the Twilio call associated with this Hume chat
        # The call SID is typically passed as part of the session or we need to look it up
        
        # Try to get call SID from multiple sources
        call_sid = None
        
        # Method 1: Try Supabase call_sessions table (for tracked inbound calls)
        if supabase_client:
            try:
                result = supabase_client.table("call_sessions").select("twilio_call_sid, chat_started_payload").eq(
                    "chat_id", chat_id
                ).order("created_at", desc=True).limit(1).execute()
                
                if result.data:
                    # Check direct twilio_call_sid field first
                    if result.data[0].get("twilio_call_sid"):
                        call_sid = result.data[0]["twilio_call_sid"]
                        print(f"[FORWARD CALL] Found call SID from session: {call_sid}")
                    else:
                        # Try to extract from chat_started_payload (Hume may include Twilio metadata)
                        payload = result.data[0].get("chat_started_payload", {})
                        if isinstance(payload, str):
                            try:
                                payload = json.loads(payload)
                            except:
                                payload = {}
                        
                        # Check common locations for Twilio call SID in Hume payload
                        call_sid = (
                            payload.get("twilio_call_sid") or
                            payload.get("call_sid") or
                            payload.get("metadata", {}).get("twilio_call_sid") or
                            payload.get("metadata", {}).get("CallSid")
                        )
                        if call_sid:
                            print(f"[FORWARD CALL] Extracted call SID from payload: {call_sid}")
                            
            except Exception as lookup_err:
                print(f"[FORWARD CALL] Error looking up call SID: {lookup_err}")
        
        # Method 2: Try to get the most recent active Twilio call to our number
        if not call_sid and twilio_client:
            try:
                # List recent calls to our Twilio number that are in-progress
                calls = twilio_client.calls.list(
                    to=TWILIO_PHONE_NUMBER,
                    status='in-progress',
                    limit=5
                )
                if calls:
                    call_sid = calls[0].sid
                    print(f"[FORWARD CALL] Found active call via Twilio API: {call_sid}")
            except Exception as twilio_lookup_err:
                print(f"[FORWARD CALL] Error looking up active calls: {twilio_lookup_err}")
        
        # If we have a call SID, redirect the call to our forward TwiML
        if call_sid:
            try:
                # Use stable production URL for Twilio callbacks (not preview deployment URL)
                # Build the TwiML URL with the forward number
                twiml_url = f"{TWILIO_CALLBACK_URL}/forward-call-twiml?forward_to={forward_to}"
                
                print(f"[FORWARD CALL] Redirecting call {call_sid} to {twiml_url}")
                
                # Update the call to redirect to our TwiML
                call = twilio_client.calls(call_sid).update(
                    url=twiml_url,
                    method="POST"
                )
                
                print(f"[FORWARD CALL] Call redirect initiated - Status: {call.status}")
                
                response_content = f"I'm transferring you now. Please hold while I connect you with our team. Transfer reason: {reason}"
                
            except Exception as twilio_err:
                print(f"[FORWARD CALL ERROR] Failed to redirect call: {twilio_err}")
                response_content = f"I apologize, but I had trouble transferring your call. Please hold and I'll try again, or you can call our office directly. Error details have been logged."
        else:
            # No call SID found - provide the staff number directly
            print(f"[FORWARD CALL] No call SID found for chat {chat_id} - providing direct number")
            response_content = f"I'd be happy to connect you with our team! Please note down this number: {CALL_FORWARD_NUMBER}. You can call them directly and they'll be able to assist you right away. Is there anything else I can help you with in the meantime?"
        
        # Send the response back to Hume
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolResponseMessage(
                tool_call_id=tool_call_id,
                content=response_content
            )
        )
        print(f"[SUCCESS] Forward call tool completed!")
        
    except Exception as e:
        print(f"[ERROR] Failed to handle forward call tool: {e}")
        
        # Send error response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolErrorMessage(
                tool_call_id=tool_call_id,
                error="CallForwardError",
                content=f"I'm having trouble transferring your call right now. Please try again or call our office directly at {CALL_FORWARD_NUMBER}. Our team will be happy to assist you."
            )
        )

async def handle_get_reminder_context_tool(control_plane_client: AsyncControlPlaneClient, chat_id: str, tool_call_message: ToolCallMessage, custom_session_id: str = None):
    """
    Handle the get_reminder_context tool call for outbound reminder calls.
    This tool fetches patient and appointment details so the AI knows who it's calling.
    
    Args:
        control_plane_client: The control plane client instance
        chat_id: The ID of the chat
        tool_call_message: The tool call message
        custom_session_id: The appointment ID passed from the webhook event (may be None for Twilio calls)
    """
    tool_call_id = tool_call_message.tool_call_id
    tool_name = tool_call_message.name
    
    print(f"[TOOL] Processing tool: {tool_name}")
    print(f"[TOOL] Tool call ID: {tool_call_id}")
    print(f"[TOOL] Custom session ID from event: {custom_session_id}")
    
    if tool_name != "get_reminder_context":
        print(f"[ERROR] Unknown tool: {tool_name}")
        return
    
    try:
        appointment_id = custom_session_id
        
        # If no custom_session_id, find the active outbound call
        # Twilio's statusCallback updates the status when call is answered:
        # - 'calling' = call initiated, ringing
        # - 'in_progress' = call answered (set by Twilio statusCallback)
        if not appointment_id and supabase_client:
            print("[REMINDER CONTEXT] No custom_session_id, looking up active outbound call...")
            try:
                # First try to find 'in_progress' (call answered)
                result = supabase_client.table("outbound_calls") \
                    .select("appointment_id") \
                    .eq("status", "in_progress") \
                    .order("last_attempt_at", desc=True) \
                    .limit(1) \
                    .execute()
                
                if result.data and len(result.data) > 0:
                    appointment_id = result.data[0]['appointment_id']
                    print(f"[REMINDER CONTEXT] Found in_progress appointment: {appointment_id}")
                else:
                    # Fallback: check for 'calling' status (in case statusCallback is slightly delayed)
                    result = supabase_client.table("outbound_calls") \
                        .select("appointment_id") \
                        .eq("status", "calling") \
                        .order("last_attempt_at", desc=True) \
                        .limit(1) \
                        .execute()
                    
                    if result.data and len(result.data) > 0:
                        appointment_id = result.data[0]['appointment_id']
                        print(f"[REMINDER CONTEXT] Found calling appointment (statusCallback pending): {appointment_id}")
            except Exception as lookup_err:
                print(f"[REMINDER CONTEXT] Error looking up active call: {lookup_err}")
        
        print(f"[REMINDER CONTEXT] Looking up appointment: {appointment_id}")
        
        if not appointment_id:
            # No way to identify the call - use fallback
            fallback_msg = "I'm calling from Sabastian Demo Practice with a friendly reminder about your upcoming dental appointment. Can you confirm you'll be able to make it?"
            await safe_send_to_control_plane(
                control_plane_client,
                chat_id,
                ToolResponseMessage(
                    tool_call_id=tool_call_id,
                    content=f"NO_CONTEXT_AVAILABLE. Use this fallback greeting: {fallback_msg}"
                )
            )
            return
        
        # Get the reminder context
        result = await get_reminder_context(appointment_id)
        
        # Format response for voice agent
        if result["success"]:
            response_content = f"""REMINDER CALL CONTEXT:
- Patient Name: {result['patient_name']}
- Patient First Name: {result['patient_first_name']}
- Appointment Time: {result['appointment_time']}
- Provider: {result['provider_name']}

Now greet the patient warmly using their first name and remind them about their appointment."""
        else:
            response_content = f"I couldn't retrieve the appointment details ({result.get('error', 'unknown error')}). Please greet the caller warmly and mention this is a reminder call from Sabastian Demo Practice about their upcoming dental appointment."
        
        # Send the result as a tool response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolResponseMessage(
                tool_call_id=tool_call_id,
                content=response_content
            )
        )
        print(f"[SUCCESS] Reminder context retrieved successfully!")
        
    except Exception as e:
        print(f"[ERROR] Failed to handle get reminder context tool: {e}")
        
        # Send error response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolErrorMessage(
                tool_call_id=tool_call_id,
                error="ReminderContextError",
                content="I'm having trouble retrieving the appointment details. Please greet the caller warmly and mention this is a reminder call from Sabastian Demo Practice."
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
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolResponseMessage(
                tool_call_id=tool_call_id,
                content=joke
            )
        )
        print(f"[SUCCESS] Dad joke sent successfully!")
        
    except Exception as e:
        print(f"[ERROR] Failed to handle dad joke tool: {e}")
        
        # Send error response
        await safe_send_to_control_plane(
            control_plane_client,
            chat_id,
            ToolErrorMessage(
                tool_call_id=tool_call_id,
                error="DadJokeError",
                content=f"Sorry, I couldn't generate a dad joke right now: {str(e)}"
            )
        )

@app.get("/")
async def root():
    """Root endpoint - confirms webhook is running."""
    return JSONResponse({
        "status": "running",
        "service": "Hume EVI Dental Assistant Webhook",
        "version": "1.0.0",
        "endpoints": {
            "webhook": "/hume-webhook",
            "health": "/health"
        }
    })

@app.get("/health")
async def health():
    """Health check endpoint."""
    return JSONResponse({
        "status": "ok", 
        "service": "Hume EVI Dental Assistant Webhook",
        "timestamp": time.time()
    })

@app.post("/trigger-outbound-calls")
async def trigger_outbound_calls(request: Request):
    """
    Trigger processing of pending outbound calls.
    This endpoint is designed to be called by a cron job.
    
    Optional query params:
    - hours_before: Hours before appointment to make the call (default: 24)
    - start_hour: Start of calling hours in local time (default: 9)
    - end_hour: End of calling hours in local time (default: 19)
    - api_key: Optional API key for authentication
    """
    # Optional: Add simple API key authentication
    params = request.query_params
    api_key = params.get("api_key")
    
    # You can add authentication here if needed
    # if api_key != os.getenv("CRON_API_KEY"):
    #     raise HTTPException(status_code=401, detail="Unauthorized")
    
    hours_before = int(params.get("hours_before", 24))
    start_hour = int(params.get("start_hour", 9))
    end_hour = int(params.get("end_hour", 19))
    
    print(f"[CRON] Triggering outbound calls - hours_before={hours_before}, calling_hours=({start_hour}, {end_hour})")
    
    result = await process_pending_outbound_calls(
        hours_before=hours_before,
        calling_hours=(start_hour, end_hour)
    )
    
    return JSONResponse(result)

@app.post("/test-outbound-call")
async def test_outbound_call(request: Request):
    """
    Test endpoint to make a single outbound call.
    
    Query params:
    - to: Phone number to call (required)
    """
    params = request.query_params
    to_number = params.get("to")
    
    if not to_number:
        raise HTTPException(status_code=400, detail="Missing 'to' parameter - phone number required")
    
    print(f"[TEST CALL] Making test call to {to_number}")
    
    result = make_outbound_call(to_number=to_number)
    
    return JSONResponse(result)

@app.post("/twilio-status")
async def twilio_status_callback(request: Request):
    """
    Handle Twilio status callbacks for outbound calls.
    
    Twilio sends these events:
    - 'answered': Call was answered by the patient
    - 'completed': Call has ended
    
    We pass appointment_id as a query parameter when making the call,
    so we can update the correct record in the outbound_calls table.
    """
    try:
        # Get appointment_id from query params (we passed it in the statusCallback URL)
        params = request.query_params
        appointment_id = params.get("appointment_id")
        
        # Parse form data from Twilio (they send POST with form data)
        form_data = await request.form()
        call_status = form_data.get("CallStatus")
        call_sid = form_data.get("CallSid")
        
        print(f"[TWILIO STATUS] Received callback - Status: {call_status}, SID: {call_sid}, Appointment: {appointment_id}")
        
        if not appointment_id:
            print("[TWILIO STATUS] Warning: No appointment_id in callback")
            return JSONResponse({"status": "ok", "warning": "no appointment_id"})
        
        if not supabase_client:
            print("[TWILIO STATUS] Warning: Supabase client not available")
            return JSONResponse({"status": "ok", "warning": "supabase unavailable"})
        
        # Update the outbound_calls record based on status
        if call_status == "answered":
            # Call was answered - set to in_progress so get_reminder_context can find it
            print(f"[TWILIO STATUS] Call ANSWERED - Setting appointment {appointment_id} to in_progress")
            supabase_client.table("outbound_calls").update({
                "status": "in_progress",
                "updated_at": datetime.utcnow().isoformat()
            }).eq("appointment_id", appointment_id).execute()
            
        elif call_status == "completed":
            # Call has ended - mark as completed
            print(f"[TWILIO STATUS] Call COMPLETED - Setting appointment {appointment_id} to completed")
            supabase_client.table("outbound_calls").update({
                "status": "completed",
                "updated_at": datetime.utcnow().isoformat()
            }).eq("appointment_id", appointment_id).execute()
            
        elif call_status in ["busy", "no-answer", "failed", "canceled"]:
            # Call failed - reset to pending for retry or mark as failed
            print(f"[TWILIO STATUS] Call {call_status.upper()} - Handling appointment {appointment_id}")
            
            # Get current call attempts
            result = supabase_client.table("outbound_calls").select("call_attempts").eq(
                "appointment_id", appointment_id
            ).execute()
            
            current_attempts = 0
            if result.data:
                current_attempts = result.data[0].get('call_attempts', 0)
            
            # If too many attempts, mark as failed; otherwise reset to pending for retry
            new_status = "failed" if current_attempts >= 3 else "pending"
            
            supabase_client.table("outbound_calls").update({
                "status": new_status,
                "updated_at": datetime.utcnow().isoformat()
            }).eq("appointment_id", appointment_id).execute()
            print(f"[TWILIO STATUS] Set appointment {appointment_id} to {new_status} (attempts: {current_attempts})")
        
        return JSONResponse({"status": "ok", "call_status": call_status, "appointment_id": appointment_id})
        
    except Exception as e:
        print(f"[TWILIO STATUS ERROR] {e}")
        # Always return 200 to Twilio to acknowledge receipt
        return JSONResponse({"status": "error", "message": str(e)})

@app.post("/forward-call-twiml")
@app.get("/forward-call-twiml")
async def forward_call_twiml(request: Request):
    """
    Generate TwiML to forward/transfer a call to the configured number (cold transfer).
    
    This endpoint is called by Twilio when we redirect a call for forwarding.
    The TwiML instructs Twilio to dial the forward number.
    """
    print(f"[FORWARD TWIML] *** Endpoint hit! Request from {request.client.host if request.client else 'unknown'} ***")
    
    try:
        # Get optional parameters from query string
        params = request.query_params
        forward_to = params.get("forward_to", CALL_FORWARD_NUMBER)
        caller_id = params.get("caller_id", TWILIO_PHONE_NUMBER)
        
        print(f"[FORWARD TWIML] Generating TwiML to forward call to {forward_to}")
        print(f"[FORWARD TWIML] Caller ID: {caller_id}")
        
        if not TwiML_VoiceResponse:
            print("[FORWARD TWIML ERROR] TwiML library not available")
            return JSONResponse(
                {"error": "TwiML library not available"}, 
                status_code=500
            )
        
        # Create TwiML response for cold transfer
        response = TwiML_VoiceResponse()
        
        # Say a brief message before transfer
        response.say(
            "Please hold while I transfer your call.",
            voice="Polly.Joanna"
        )
        
        # Dial the forward number
        # timeout: how long to wait for answer (30 seconds)
        # callerId: shows the original Twilio number to the recipient
        status_callback_url = f"{TWILIO_CALLBACK_URL}/forward-call-status"
        print(f"[FORWARD TWIML] Status callback URL: {status_callback_url}")
        
        dial = response.dial(
            timeout=30,
            caller_id=caller_id,
            action=status_callback_url
        )
        dial.number(forward_to)
        
        # If no one answers, say goodbye
        response.say(
            "I'm sorry, but no one is available to take your call right now. Please try again later or leave a message.",
            voice="Polly.Joanna"
        )
        response.hangup()
        
        twiml_str = str(response)
        print(f"[FORWARD TWIML] Generated TwiML: {twiml_str}")
        
        # Return TwiML with proper content type
        from starlette.responses import Response
        return Response(content=twiml_str, media_type="application/xml")
        
    except Exception as e:
        print(f"[FORWARD TWIML ERROR] Exception: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/forward-call-status")
async def forward_call_status(request: Request):
    """
    Handle the status callback after a call forward attempt.
    This is optional but useful for logging/tracking transfer results.
    """
    try:
        form_data = await request.form()
        dial_call_status = form_data.get("DialCallStatus")
        dial_call_sid = form_data.get("DialCallSid")
        call_sid = form_data.get("CallSid")
        
        print(f"[FORWARD STATUS] Transfer result - Status: {dial_call_status}, DialSid: {dial_call_sid}, CallSid: {call_sid}")
        
        # You could log this to Supabase if needed
        # For now, just acknowledge
        
        # Return empty TwiML (call has ended)
        if TwiML_VoiceResponse:
            response = TwiML_VoiceResponse()
            from starlette.responses import Response
            return Response(content=str(response), media_type="application/xml")
        
        return JSONResponse({"status": "ok"})
        
    except Exception as e:
        print(f"[FORWARD STATUS ERROR] {e}")
        return JSONResponse({"status": "error", "message": str(e)})

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
        
        # Log to Supabase
        await log_call_session_start(
            chat_id=event.chat_id,
            chat_group_id=getattr(event, 'chat_group_id', None),
            config_id=getattr(event, 'config_id', None),
            caller_number=getattr(event, 'caller_number', None),
            full_payload=event.dict()
        )
        
    elif isinstance(event, WebhookEventChatEnded):
        print(f"[CHAT] Chat ended: {event.chat_id}")
        print(f"[CHAT] Event data: {event.dict()}")
        
        # Log to Supabase
        await log_call_session_end(
            chat_id=event.chat_id,
            full_payload=event.dict()
        )
        
        # If this is an outbound call (from our outbound config), mark it as completed
        config_id = getattr(event, 'config_id', None)
        if config_id == HUME_OUTBOUND_CONFIG_ID and supabase_client:
            try:
                # Find the most recent in_progress call and mark it completed
                result = supabase_client.table("outbound_calls") \
                    .select("appointment_id") \
                    .eq("status", "in_progress") \
                    .order("last_attempt_at", desc=True) \
                    .limit(1) \
                    .execute()
                
                if result.data and len(result.data) > 0:
                    appointment_id = result.data[0]['appointment_id']
                    supabase_client.table("outbound_calls").update({
                        "status": "completed",
                        "updated_at": datetime.utcnow().isoformat()
                    }).eq("appointment_id", appointment_id).execute()
                    print(f"[CHAT ENDED] Marked outbound call {appointment_id} as completed")
            except Exception as e:
                print(f"[CHAT ENDED] Error updating outbound call status: {e}")
        
    elif isinstance(event, WebhookEventToolCall):
        print(f"[TOOL] Tool call received: {event.dict()}")
        
        # Extract custom_session_id from the event (used for outbound call context)
        custom_session_id = getattr(event, 'custom_session_id', None)
        print(f"[TOOL] Custom session ID: {custom_session_id}")
        
        # Route to appropriate tool handler based on tool name
        tool_name = event.tool_call_message.name
        
        # Map tool names to handler functions
        tool_handlers = {
            "tell_dad_joke": handle_dad_joke_tool,
            "search_patients": handle_search_patients_tool,
            "create_patient": handle_create_patient_tool,
            "get_providers": handle_get_providers_tool,
            "get_available_slots": handle_get_available_slots_tool,
            "get_locations": handle_get_locations_tool,
            "book_appointment": handle_book_appointment_tool,
            "get_patient_appointments": handle_get_patient_appointments_tool,
            "reschedule_appointment": handle_reschedule_appointment_tool,
            "forward_call": handle_forward_call_tool
        }
        
        # Special handling for get_reminder_context (needs custom_session_id)
        if tool_name == "get_reminder_context":
            await handle_get_reminder_context_tool(
                control_plane_client,
                event.chat_id,
                event.tool_call_message,
                custom_session_id  # Pass the appointment_id from the call setup
            )
        elif tool_name in tool_handlers:
            # Execute with logging
            await log_and_execute_tool(
                chat_id=event.chat_id,
                tool_call_message=event.tool_call_message,
                handler_func=tool_handlers[tool_name],
                control_plane_client=control_plane_client
            )
        else:
            print(f"[ERROR] Unknown tool: {tool_name}")
            
            # Log unknown tool call
            await log_tool_call_event(
                chat_id=event.chat_id,
                tool_call_id=event.tool_call_message.tool_call_id,
                tool_name=tool_name,
                tool_type=getattr(event.tool_call_message, 'tool_type', 'function'),
                parameters={},
                response_required=True,
                webhook_payload=event.dict()
            )
            
            await log_tool_call_result(
                tool_call_id=event.tool_call_message.tool_call_id,
                success=False,
                error_type="UnknownTool",
                error_message=f"Unknown tool: {tool_name}",
                response_type="tool_error",
                response_content=f"I don't know how to use the {tool_name} tool. Please contact support."
            )
            
            # Send error response for unknown tools
            await safe_send_to_control_plane(
                control_plane_client,
                event.chat_id,
                ToolErrorMessage(
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