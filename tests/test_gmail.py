import os
import json
from google.cloud import secretmanager
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

PROJECT_ID = "reconciler-mohammed-emad"
SA_KEY_PATH = os.path.expanduser("~/keys/reconciler-sa.json")

# 1. Authenticate Secret Manager via Service Account Key directly
sa_creds = service_account.Credentials.from_service_account_file(SA_KEY_PATH)
client = secretmanager.SecretManagerServiceClient(credentials=sa_creds)

name = f"projects/{PROJECT_ID}/secrets/reconciler-oauth-config/versions/latest"
response = client.access_secret_version(request={"name": name})
token_info = json.loads(response.payload.data.decode("UTF-8"))

# 2. Build Gmail service using the delegated user tokens from Secret Manager
user_creds = Credentials.from_authorized_user_info(token_info)
service = build('gmail', 'v1', credentials=user_creds)

# 3. Test read operation
results = service.users().messages().list(userId='me', maxResults=1).execute()
messages = results.get('messages', [])

if messages:
    msg = service.users().messages().get(userId='me', id=messages[0]['id']).execute()
    headers = {h['name']: h['value'] for h in msg['payload']['headers']}
    print(f"[+] Gmail Access Verified! Latest Subject: {headers.get('Subject')}")
else:
    print("[+] Gmail Access Verified! (Inbox is empty)")