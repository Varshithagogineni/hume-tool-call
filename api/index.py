"""
Vercel serverless entry point for Hume webhook
This file properly imports the FastAPI app for Vercel deployment
"""
import sys
import os

# Add the parent directory to Python path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

# Import the FastAPI app
# Since hume_webhook.py is in the parent directory, we can import it directly
import hume_webhook

# Export the app variable for Vercel
app = hume_webhook.app
