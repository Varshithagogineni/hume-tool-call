# Hume EVI Dad Joke Webhook

A FastAPI webhook server that handles tool calls from Hume's Empathic Voice Interface (EVI) to deliver dad jokes.

## Features

- 🎭 **Dad Joke Generator**: Random dad jokes delivered through EVI
- 🔗 **Webhook Integration**: Receives tool_call events from Hume EVI  
- 📡 **Hume API Integration**: Uses official Hume SDK and ControlPlaneClient
- 🚀 **FastAPI**: High-performance async webhook server
- 🛡️ **Error Handling**: Robust error handling and logging

## Setup

### Prerequisites
- Python 3.11+
- Hume AI API Key
- EVI configuration with `tell_dad_joke` tool

### Installation

1. Clone the repository:
```bash
git clone <your-repo-url>
cd evi-dad-joke-webhook
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Set your Hume API key in `hume_webhook.py` or use environment variables.

4. Run the server:
```bash
python hume_webhook.py
```

## Deployment

### Railway (Recommended)
1. Connect this GitHub repo to Railway
2. Set environment variable: `HUME_API_KEY=your_key`
3. Railway auto-deploys on push

### Other Platforms
- **Render**: Connect GitHub repo, auto-deploy
- **Google Cloud Run**: Deploy as container
- **Vercel**: Serverless function deployment

## API Endpoints

- `POST /hume-webhook` - Main webhook endpoint for Hume EVI
- `GET /health` - Health check endpoint

## Tool Configuration

Your EVI config should include:
```json
{
  "tools": [{
    "id": "your-dad-joke-tool-id"
  }],
  "webhooks": [{
    "url": "https://your-deployed-url.com/hume-webhook",
    "events": ["tool_call"]
  }]
}
```

## Usage

Once deployed and configured:
1. User asks EVI: "Tell me a dad joke"
2. EVI triggers the `tell_dad_joke` tool
3. Webhook receives the tool call
4. Server generates random dad joke
5. Joke is sent back to EVI via ControlPlaneClient
6. EVI speaks the dad joke to the user

## Contributing

Pull requests welcome! Please ensure tests pass and follow the existing code style.
