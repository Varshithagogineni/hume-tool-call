# Hume EVI Dental Assistant Webhook

A FastAPI webhook server that integrates Hume's Empathic Voice Interface (EVI) with NexHealth/Syncronizer.io to provide AI-powered voice appointment management for dental practices.

## Overview

This system enables patients to manage dental appointments through natural voice conversations. The webhook processes tool calls from Hume EVI, interacts with the NexHealth practice management API, and returns structured responses that the voice AI speaks back to callers.

### Key Problem Solved

Dental practices handle high volumes of routine phone calls for scheduling, rescheduling, and confirming appointments. This solution automates these interactions through an AI voice assistant that:

- Reduces front desk workload
- Provides 24/7 appointment availability
- Maintains consistent patient experience
- Logs all interactions for compliance and analytics

---

## Features

- **Patient Search**: Look up patients by name, phone, email, or date of birth
- **Patient Registration**: Create new patient records with validated information
- **Appointment Booking**: Schedule appointments with availability checking
- **Appointment Rescheduling**: Modify or cancel existing appointments
- **Provider Lookup**: Retrieve available dentists and hygienists
- **Availability Search**: Find open appointment slots across providers
- **Location Management**: Access practice location information
- **Outbound Reminder Calls**: Automated appointment confirmation calls
- **Call Forwarding**: Transfer callers to staff when needed
- **Event Logging**: Comprehensive audit trail in Supabase

---

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌────────────────────┐
│                 │     │                  │     │                    │
│  Patient Phone  │────▶│  Twilio          │────▶│  Hume EVI          │
│                 │     │  (Voice Gateway) │     │  (Voice AI)        │
└─────────────────┘     └──────────────────┘     └─────────┬──────────┘
                                                           │
                                                           │ Webhook Events
                                                           ▼
┌─────────────────┐     ┌──────────────────┐     ┌────────────────────┐
│                 │     │                  │     │                    │
│  NexHealth API  │◀────│  This Webhook    │◀────│  FastAPI Server    │
│  (Syncronizer)  │     │  (Tool Handler)  │     │                    │
└─────────────────┘     └──────────────────┘     └─────────┬──────────┘
                                                           │
                                                           │ Logging
                                                           ▼
                                                ┌────────────────────┐
                                                │                    │
                                                │  Supabase          │
                                                │  (Event Store)     │
                                                └────────────────────┘
```

### Data Flow

1. Patient calls the Twilio phone number
2. Twilio routes the call to Hume EVI
3. Hume EVI processes speech and generates tool calls
4. This webhook receives tool call events via POST
5. Webhook executes the appropriate NexHealth API calls
6. Results are sent back to Hume via the Control Plane API
7. Hume EVI speaks the response to the patient
8. All events are logged to Supabase

---

## Tech Stack

| Component | Technology | Purpose |
|-----------|------------|---------|
| Web Framework | FastAPI | Async webhook server |
| Voice AI | Hume EVI | Speech recognition and synthesis |
| Telephony | Twilio | Inbound/outbound call handling |
| Practice Management | NexHealth (Syncronizer.io) | Patient and appointment data |
| Database | Supabase | Event logging and call tracking |
| Deployment | Vercel | Serverless or container hosting |

---

## Prerequisites

- Python 3.11 or higher
- Hume AI account with EVI access
- Twilio account with voice-enabled phone number
- NexHealth/Syncronizer.io API credentials
- Supabase project (optional, for logging)

---

## Installation

1. Clone the repository:

```bash
git clone <repository-url>
cd hume-tool-call
```

2. Create a virtual environment:

```bash
python -m venv venv
source venv/bin/activate  # Linux/macOS
venv\Scripts\activate     # Windows
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Configure environment variables (see Configuration section)

5. Run the development server:

```bash
python hume_webhook.py
```

The server starts on `http://127.0.0.1:5000` by default.

---

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `HUME_API_KEY` | Yes | Hume AI API key |
| `HUME_CONFIG_ID` | Yes | EVI configuration ID for inbound calls |
| `HUME_OUTBOUND_CONFIG_ID` | No | EVI configuration ID for outbound reminder calls |
| `SYNCRONIZER_API_KEY` | Yes | NexHealth/Syncronizer API key |
| `SYNCRONIZER_SUBDOMAIN` | Yes | Practice subdomain in NexHealth |
| `SYNCRONIZER_LOCATION_ID` | Yes | Location ID for the practice |
| `SYNCRONIZER_BASE_URL` | Yes | NexHealth API base URL |
| `TWILIO_ACCOUNT_SID` | No | Twilio account SID (for outbound calls) |
| `TWILIO_AUTH_TOKEN` | No | Twilio auth token |
| `TWILIO_PHONE_NUMBER` | No | Twilio phone number in E.164 format |
| `TWILIO_CALLBACK_URL` | No | Stable URL for Twilio callbacks (production URL) |
| `CALL_FORWARD_NUMBER` | No | Phone number for call transfers |
| `SUPABASE_URL` | No | Supabase project URL |
| `SUPABASE_KEY` | No | Supabase service role key |
| `OUTBOUND_TEST_MODE` | No | Set to `true` to bypass time checks for testing |
| `PORT` | No | Server port (default: 5000) |

### Hume EVI Configuration

Your EVI configuration must include the following tools:

```json
{
  "tools": [
    { "name": "search_patients" },
    { "name": "create_patient" },
    { "name": "get_providers" },
    { "name": "get_available_slots" },
    { "name": "get_locations" },
    { "name": "book_appointment" },
    { "name": "get_patient_appointments" },
    { "name": "reschedule_appointment" },
    { "name": "forward_call" },
    { "name": "get_reminder_context" }
  ],
  "webhooks": [{
    "url": "https://your-domain.com/hume-webhook",
    "events": ["tool_call", "chat_started", "chat_ended"]
  }]
}
```

### Supabase Schema

If using Supabase for logging, create these tables:

**call_sessions**
```sql
CREATE TABLE call_sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  chat_id TEXT UNIQUE NOT NULL,
  chat_group_id TEXT,
  config_id TEXT,
  caller_number TEXT,
  started_at TIMESTAMPTZ,
  ended_at TIMESTAMPTZ,
  status TEXT DEFAULT 'active',
  chat_started_payload JSONB,
  chat_ended_payload JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);
```

**tool_call_events**
```sql
CREATE TABLE tool_call_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  chat_id TEXT NOT NULL,
  tool_call_id TEXT UNIQUE NOT NULL,
  tool_name TEXT NOT NULL,
  tool_type TEXT,
  parameters JSONB,
  response_required BOOLEAN,
  called_at TIMESTAMPTZ,
  execution_started_at TIMESTAMPTZ,
  execution_completed_at TIMESTAMPTZ,
  execution_time_ms INTEGER,
  success BOOLEAN,
  result_summary TEXT,
  result_data JSONB,
  error_type TEXT,
  error_message TEXT,
  error_detail JSONB,
  response_type TEXT,
  response_content TEXT,
  response_sent_at TIMESTAMPTZ,
  webhook_payload JSONB,
  sequence_number INTEGER,
  created_at TIMESTAMPTZ DEFAULT now()
);
```

**outbound_calls**
```sql
CREATE TABLE outbound_calls (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  appointment_id TEXT UNIQUE NOT NULL,
  patient_id TEXT,
  provider_id TEXT,
  phone_number TEXT NOT NULL,
  appointment_time TIMESTAMPTZ,
  timezone TEXT DEFAULT 'America/New_York',
  status TEXT DEFAULT 'pending',
  call_sid TEXT,
  call_attempts INTEGER DEFAULT 0,
  last_attempt_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);
```

---

## Usage

### API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Service info and available endpoints |
| `GET` | `/health` | Health check |
| `POST` | `/hume-webhook` | Main webhook for Hume EVI events |
| `POST` | `/trigger-outbound-calls` | Trigger pending reminder calls (for cron) |
| `POST` | `/test-outbound-call` | Test a single outbound call |
| `POST` | `/twilio-status` | Twilio call status callbacks |
| `POST` | `/forward-call-twiml` | TwiML for call forwarding |
| `POST` | `/forward-call-status` | Forward call status callback |

### Example Conversations

**Booking an Appointment**
```
Patient: "I'd like to schedule a cleaning"
EVI: [Searches for patient] "I found your record. Let me check available times..."
EVI: [Gets available slots] "I have openings on Tuesday at 2 PM or Thursday at 10 AM"
Patient: "Thursday works"
EVI: [Books appointment] "Perfect, you're all set for Thursday at 10 AM with Dr. Smith"
```

**Rescheduling**
```
Patient: "I need to move my appointment next week"
EVI: [Gets patient appointments] "I see you have an appointment on Tuesday at 3 PM"
Patient: "Can I move it to Wednesday?"
EVI: [Gets available slots, reschedules] "Done. Your appointment is now Wednesday at 3 PM"
```

### Outbound Reminder Calls

To process pending reminder calls, set up a cron job or scheduler to call:

```bash
curl -X POST "https://your-domain.com/trigger-outbound-calls?hours_before=24&start_hour=9&end_hour=19"
```

Query parameters:
- `hours_before`: Hours before appointment to call (default: 24)
- `start_hour`: Start of calling hours in local time (default: 9)
- `end_hour`: End of calling hours in local time (default: 19)

---

## Project Structure

```
hume-tool-call/
├── api/
│   └── index.py              # Vercel serverless entry point
├── hume_webhook.py           # Main application (all handlers)
├── requirements.txt          # Python dependencies
├── Dockerfile                # Container configuration
├── vercel.json               # Vercel deployment config
└── README.md                 # This file
```

### Module Overview

The `hume_webhook.py` file contains:

| Section | Lines (approx) | Description |
|---------|----------------|-------------|
| Configuration | 1-100 | Environment variables, client initialization |
| Supabase Logging | 100-375 | Event logging functions |
| NexHealth API | 375-1000 | Patient, appointment, provider functions |
| Tool Handlers | 2250-3600 | Individual tool implementations |
| HTTP Endpoints | 3600-3982 | FastAPI route definitions |

---

## Deployment

### Vercel

1. Install Vercel CLI:

```bash
npm install -g vercel
```

2. Deploy:

```bash
vercel --prod
```

3. Set environment variables in Vercel dashboard

The `vercel.json` configures the Python serverless function with a 15MB limit.

### Docker

1. Build the image:

```bash
docker build -t hume-dental-webhook .
```

2. Run the container:

```bash
docker run -p 8080:8080 \
  -e HUME_API_KEY=your_key \
  -e SYNCRONIZER_API_KEY=your_key \
  # ... other env vars
  hume-dental-webhook
```

### Railway / Render

Connect the repository directly. Both platforms auto-detect the Dockerfile and deploy accordingly.

---

## Development Notes

### Authentication Flow

The webhook uses a cached bearer token for NexHealth API calls:
1. Initial authentication with API key returns a bearer token
2. Token is cached with 50-minute expiry (actual expiry is 60 minutes)
3. `get_bearer_token()` automatically refreshes when expired

### Error Handling

- All tool handlers catch exceptions and return user-friendly error messages
- Chat unavailability errors (e.g., caller hung up) are handled gracefully
- API timeouts default to 10 seconds with appropriate retry logic
- Failed outbound calls retry up to 3 times

### Logging Strategy

The system logs to both console and Supabase:
- Console logs use `[PREFIX]` format for easy filtering
- Supabase stores complete payloads for debugging
- Authorization headers are redacted from logged data

---

## Limitations and Assumptions

- **Single Location**: Currently configured for a single practice location
- **US Phone Numbers**: Phone number formatting assumes US (+1) country code
- **Timezone Handling**: Defaults to `America/New_York` if not specified
- **Appointment Duration**: Defaults to 1-hour slots if `end_time` not provided
- **NexHealth Version**: Uses API version `v20240412`
- **Twilio Dependency**: Outbound calls require Twilio; gracefully disabled if not configured

---

## License

Proprietary. All rights reserved.
