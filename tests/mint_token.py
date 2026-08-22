import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.send',   # P13: approve-and-send beat
    'https://www.googleapis.com/auth/drive',
]

CLIENT_SECRET_FILE = os.path.expanduser('~/keys/client_secret_542923033636-bfvjbjfkuubee3fljk7tmr03fqfe8pds.apps.googleusercontent.com.json')
OUTPUT_TOKEN_FILE = os.path.expanduser('~/keys/oauth_tokens.json')

def mint_token():
    flow = InstalledAppFlow.from_client_secrets_file(
        CLIENT_SECRET_FILE,
        scopes=SCOPES
    )
    creds = flow.run_local_server(port=0, prompt='consent', access_type='offline')

    # creds.to_json() returns the full JSON string needed to reconstruct Credentials
    with open(OUTPUT_TOKEN_FILE, 'w') as f:
        f.write(creds.to_json())

    print("\n[+] Refresh token minted successfully!")
    print(f"[+] Refresh Token: {creds.refresh_token}")
    print(f"[+] Credentials saved to: {OUTPUT_TOKEN_FILE}")

if __name__ == '__main__':
    mint_token()