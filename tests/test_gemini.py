import os
from google import genai

# Initialize client using Google AI Studio API key
api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

response = client.models.generate_content(
    model="gemini-3.5-flash",
    contents="Ping. Confirm you are Gemini 3.5 Flash."
)

print("[+] API Key Verified!")
print(f"[+] Output: {response.text.strip()}")