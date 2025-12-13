import os
import io
import time
import razorpay
import jwt
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, send_file, render_template, request, abort, url_for
from dotenv import load_dotenv  # <--- IMPORT THIS

# Google Drive Imports
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# 1. LOAD ENVIRONMENT VARIABLES
load_dotenv()  # This reads the .env file

app = Flask(__name__)

# --- CONFIGURATION (Now Secure) ---
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
app.config['JWT_SECRET'] = os.getenv('JWT_SECRET')

# Get Razorpay Keys
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID')
RAZORPAY_SECRET = os.getenv('RAZORPAY_SECRET')

# Get Google Drive File Path
SERVICE_ACCOUNT_FILE = os.getenv('GDRIVE_KEY_FILE', 'gdrive_service_account.json')
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

# Initialize Razorpay
# We add a check to warn you if keys are missing
if not RAZORPAY_KEY_ID or not RAZORPAY_SECRET:
    print("⚠️ WARNING: Razorpay Keys are missing in .env file!")

razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_SECRET))


# --- 2. PRODUCT DATABASE ---
# Maps Bundle ID -> { Folder ID, Price (in paise), Name }
BUNDLES = {
    "ds": {
        "name": "Data Structures (DS)",
        "folder_id": "1cDkiM7rb_C6kwm2dvl-ZOgITqGlLnOUj", # Your working ID
        "price": 1900  # ₹19.00 (in paise)
    },
    "oop_cg": {
        "name": "OOP & CG",
        "folder_id": "REPLACE_WITH_OOP_FOLDER_ID",
        "price": 4900 # ₹49.00
    }
}

# --- 3. GOOGLE DRIVE HELPER ---
def get_gdrive_service():
    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"Drive Auth Error: {e}")
        return None

def get_files_from_drive(folder_id):
    service = get_gdrive_service()
    if not service: return []
    query = f"'{folder_id}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    return results.get('files', [])

# --- 4. JWT SECURITY HELPER ---
def create_access_token(bundle_id):
    """Creates a token valid for 2 hours."""
    expiration = datetime.now(timezone.utc) + timedelta(hours=2)
    token = jwt.encode({
        'bundle_id': bundle_id,
        'exp': expiration
    }, app.config['JWT_SECRET'], algorithm='HS256')
    return token

def verify_token(token):
    try:
        data = jwt.decode(token, app.config['JWT_SECRET'], algorithms=['HS256'])
        return data # Returns dictionary { 'bundle_id': 'ds', ... }
    except:
        return None

# --- 5. ROUTES ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/bundle.html')
def bundle():
    return render_template('bundle.html')

@app.route('/api/files/<bundle_id>')
def api_get_files(bundle_id):
    bundle = BUNDLES.get(bundle_id)
    if not bundle: return jsonify({"error": "Invalid Bundle"}), 404
    files = get_files_from_drive(bundle['folder_id'])
    return jsonify({"files": files})

# --- RAZORPAY: STEP 1 (Create Order) ---
@app.route('/create_order', methods=['POST'])
def create_order():
    data = request.json
    bundle_id = data.get('bundle_id')
    bundle = BUNDLES.get(bundle_id)
    
    if not bundle: return jsonify({'error': 'Invalid Bundle'}), 400

    order = razorpay_client.order.create({
        'amount': bundle['price'],
        'currency': 'INR',
        'payment_capture': '1'
    })
    
    return jsonify({
        'order_id': order['id'], 
        'key_id': RAZORPAY_KEY_ID,
        'amount': bundle['price']
    })

# --- RAZORPAY: STEP 2 (Verify & Token) ---
@app.route('/verify_payment', methods=['POST'])
def verify_payment():
    data = request.json
    try:
        # Check signature
        razorpay_client.utility.verify_payment_signature({
            'razorpay_order_id': data['razorpay_order_id'],
            'razorpay_payment_id': data['razorpay_payment_id'],
            'razorpay_signature': data['razorpay_signature']
        })
        
        # Payment Valid! Generate Access Token
        token = create_access_token(data['bundle_id'])
        
        return jsonify({'status': 'success', 'token': token})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

# --- SECURE DOWNLOAD ---
@app.route('/download/<file_id>')
def download_file(file_id):
    # 1. GET TOKEN FROM URL (Uses Flask's 'request')
    token = request.args.get('token')
    
    # Check if token is valid
    if not token or not verify_token(token):
        return "❌ ACCESS DENIED: Payment Required or Link Expired.", 403

    # 2. IF VALID, DOWNLOAD
    service = get_gdrive_service()
    if not service:
        return "Service Unavailable", 500

    try:
        # Get File Name
        meta = service.files().get(fileId=file_id).execute()
        file_name = meta.get('name', 'download.pdf')
        
        # --- FIX IS HERE: Renamed variable to 'drive_request' ---
        drive_request = service.files().get_media(fileId=file_id)
        
        file_stream = io.BytesIO()
        # Pass the renamed variable here
        downloader = MediaIoBaseDownload(file_stream, drive_request)
        
        done = False
        while not done:
            _, done = downloader.next_chunk()
            
        file_stream.seek(0)
        
        return send_file(
            file_stream, 
            as_attachment=True, 
            download_name=file_name
        )
    except Exception as e:
        print(f"Download Error: {e}")
        return f"Error: {e}", 500

if __name__ == '__main__':
    app.run(debug=True)
