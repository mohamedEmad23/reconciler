# test_vertex.py
import os
import vertexai
from vertexai.generative_models import GenerativeModel

PROJECT_ID = "reconciler-mohammed-emad"
LOCATION = "us-central1"

vertexai.init(project=PROJECT_ID, location=LOCATION)

# Test primary flash model endpoint
model_id = "gemini-3.5-flash"
try:
    model = GenerativeModel(model_id)
    response = model.generate_content("Ping. Respond with 'PONG'.")
    print(f"[+] Vertex AI OK | Model: {model_id} | Response: {response.text.strip()}")
except Exception as e:
    # Fallback to standard 2.0 / 1.5 endpoint if region differs
    fallback_id = "gemini-2.0-flash"
    model = GenerativeModel(fallback_id)
    response = model.generate_content("Ping. Respond with 'PONG'.")
    print(f"[+] Vertex AI OK | Model: {fallback_id} | Response: {response.text.strip()}")