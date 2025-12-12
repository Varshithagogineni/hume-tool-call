# This file re-exports the FastAPI app from hume_webhook.py
# Vercel looks for 'app' in api/index.py

import sys
import os

# Add parent directory to path to enable imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Now we can import from the hume-tool-call directory
# We use exec to handle the hyphenated directory name
webhook_file = os.path.join(os.path.dirname(__file__), '..', 'hume-tool-call', 'hume_webhook.py')

# Read and execute the webhook file
with open(webhook_file, 'r', encoding='utf-8') as f:
    code = f.read()
    exec(code, globals())

# The 'app' variable is now available from the executed code

