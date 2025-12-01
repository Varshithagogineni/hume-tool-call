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
        await control_plane_client.send(
            chat_id=chat_id,
            request=ToolResponseMessage(
                tool_call_id=tool_call_id,
                content=response_content
            )
        )
        print(f"[SUCCESS] Available slots search completed successfully!")
        
    except Exception as e:
        print(f"[ERROR] Failed to handle get available slots tool: {e}")
        
        # Send error response
        await control_plane_client.send(
            chat_id=chat_id,
            request=ToolErrorMessage(
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
        await control_plane_client.send(
            chat_id=chat_id,
            request=ToolResponseMessage(
                tool_call_id=tool_call_id,
                content=response_content
            )
        )
        print(f"[SUCCESS] Location search completed successfully!")
        
    except Exception as e:
        print(f"[ERROR] Failed to handle get locations tool: {e}")
        
        # Send error response
        await control_plane_client.send(
            chat_id=chat_id,
            request=ToolErrorMessage(
                tool_call_id=tool_call_id,
                error="LocationSearchError",
                content=f"I'm having trouble finding location information right now. Please try again or contact our office directly. Error: {str(e)}"
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
        elif tool_name == "get_available_slots":
            await handle_get_available_slots_tool(control_plane_client, event.chat_id, event.tool_call_message)
        elif tool_name == "get_locations":
            await handle_get_locations_tool(control_plane_client, event.chat_id, event.tool_call_message)
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