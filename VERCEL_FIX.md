# Fix for Vercel 500 Error

## ✅ What I Fixed

### **Problem:**
The serverless function was crashing with `FUNCTION_INVOCATION_FAILED` because:
- `api/index.py` was using `exec()` which doesn't work well in serverless
- Path issues in the serverless environment

### **Solution:**
Updated `api/index.py` to use proper Python imports:

```python
import sys
import os

# Add parent directory to path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

# Import the app
import hume_webhook
app = hume_webhook.app
```

---

## 🚀 Deploy Now

### 1. Commit and Push:
```bash
git add api/ vercel.json
git commit -m "Fix Vercel serverless function crash"
git push origin main
```

### 2. Vercel will auto-redeploy

### 3. Check Logs:
Go to Vercel dashboard → Your project → Functions → View logs

---

## 🧪 Test After Deployment

```bash
# Test root endpoint
curl https://YOUR_PROJECT.vercel.app/

# Expected response:
# {"status":"running","service":"Hume EVI Dental Assistant Webhook",...}
```

---

## 🐛 If Still Fails

### Check Function Logs in Vercel:
1. Go to your Vercel project
2. Click "Functions" tab
3. Click on the failed function
4. View the error logs

### Common Issues:

**1. Import Errors:**
- Make sure `hume_webhook.py` is in the root directory
- Check that all dependencies are in `requirements.txt`

**2. Environment Variables:**
- Go to Project Settings → Environment Variables
- Make sure `HUME_API_KEY` is set
- Redeploy after adding variables

**3. Cold Start Timeout:**
- Vercel free tier: 10 second timeout
- If your imports are heavy, consider:
  - Lazy loading
  - Upgrade to Pro plan (60s timeout)

**4. Module Not Found:**
- Check the function logs for the exact error
- The path might need adjustment

---

## 📊 Project Structure

Your repo should look like:

```
hume-tool-call/  (git root)
├── api/
│   └── index.py        ← Vercel entry point
├── hume_webhook.py     ← Your FastAPI app
├── vercel.json         ← Vercel config
├── requirements.txt    ← Dependencies
├── .gitignore
└── README.md
```

---

## ⚡ Alternative: Simpler Approach

If imports still fail, create a flat structure:

```bash
# Move hume_webhook.py into api/
mv hume_webhook.py api/app.py

# Update vercel.json:
{
  "builds": [{"src": "api/app.py", "use": "@vercel/python"}],
  "routes": [{"src": "/(.*)", "dest": "api/app.py"}]
}
```

---

## 💡 Best Alternative: Use Render.com

Vercel isn't ideal for FastAPI. **Render.com is much easier:**

1. Go to https://render.com
2. Sign in with GitHub
3. New → Web Service
4. Connect your repo
5. Settings:
   ```
   Environment: Python 3
   Build Command: pip install -r requirements.txt
   Start Command: uvicorn hume_webhook:app --host 0.0.0.0 --port $PORT
   ```
6. Add `HUME_API_KEY` environment variable
7. Deploy!

**Render URL:** `https://your-app.onrender.com/hume-webhook`

---

## 🎯 Quick Checklist

- ✅ `api/index.py` uses proper import (not exec)
- ✅ `vercel.json` configured correctly
- ✅ `PYTHONPATH` set in vercel.json
- ✅ All files committed and pushed
- ✅ Environment variable added
- ✅ Check function logs if it fails

---

## 🆘 Still Having Issues?

**Switch to Render:**
- More reliable for FastAPI
- Better logging
- Simpler configuration
- Free tier is generous

Deploy to Render in 5 minutes instead of debugging Vercel for hours! 🚀


