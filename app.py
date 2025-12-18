import os
import io
import time
import razorpay
import jwt
import certifi
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, send_file, render_template, request, url_for
from dotenv import load_dotenv
from pymongo import MongoClient

# Google Drive Imports
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# 1. LOAD CONFIGURATION
load_dotenv()

app = Flask(__name__)

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev_key')
app.config['JWT_SECRET'] = os.getenv('JWT_SECRET', 'dev_jwt_key')

# Razorpay Keys
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID')
RAZORPAY_SECRET = os.getenv('RAZORPAY_SECRET')

# Google Drive Config
SERVICE_ACCOUNT_FILE = 'gdrive_service_account.json'
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

# Bundle Map
BUNDLES = {
    "ds": {
        "name": "Data Structures (DS)",
        "folder_id": "1cDkiM7rb_C6kwm2dvl-ZOgITqGlLnOUj", 
        "price": 1900 
    },
    "oop_cg": {
        "name": "OOP & CG",
        "folder_id": "1367AQPrax2YYrMk4Rz-9AVt8Qgy1lSSt", 
        "price": 1900
    },
    "os": {
        "name": "Operating Systems",
        "folder_id": "1kX5qgFguKDAfonJcPwwIUaDzwP1bi7by", 
        "price": 1900
    },
    "deld": {
        "name": "DELD",
        "folder_id": "1mbyzQ8yyCxYMPbLRUPIHM_2Uvje4pBbd", 
        "price": 1900
    },
    "DM": {
        "name": "Digital Marketing",
        "folder_id": "16L0YVmB5vxaQhaXOCQ1grZasu5sbICNZ", 
        "price": 1900
    },
    "uhvpe": {
        "name": "uhvpe",
        "folder_id": "1sNI4QX_7dA-pgycAQ0tGRdiloi8OoaTY", 
        "price": 1900
    },
}

# --- COUPON CODES ---
COUPONS = {
    "ES10": 10      # 10% Off
    #"BFF100": 100    # 100% Off (Free)
}

# --- 2. DATABASE CONNECTION ---
try:
    client = MongoClient(os.getenv('MONGO_URI'), tlsCAFile=certifi.where())
    db = client['notes_app']
    
    # Collection 1: Active Users (Temporary Access)
    access_collection = db['active_users']
    access_collection.create_index("created_at", expireAfterSeconds=7200)
    
    # Collection 2: Loyalty List (Permanent Discount List)
    loyalty_collection = db['loyalty']
    
    print("✅ MongoDB Connected & Rules Set!")
except Exception as e:
    print(f"❌ MongoDB Error: {e}")
    access_collection = None
    loyalty_collection = None

# --- 3. HELPER FUNCTIONS ---

def get_gdrive_service():
    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"Drive Auth Error: {e}")
        return None

def create_access_token(bundle_id):
    exp = datetime.now(timezone.utc) + timedelta(hours=2)
    return jwt.encode({'bundle_id': bundle_id, 'exp': exp}, app.config['JWT_SECRET'], algorithm='HS256')

def verify_token(token):
    try:
        return jwt.decode(token, app.config['JWT_SECRET'], algorithms=['HS256'])
    except:
        return None

# --- 4. ROUTES ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/bundle.html')
def bundle():
    bundle_id = request.args.get('id', 'ds')
    product = BUNDLES.get(bundle_id)
    return render_template('bundle.html', product=product, bundle_id=bundle_id)

@app.route('/api/files/<bundle_id>')
def api_get_files(bundle_id):
    bundle = BUNDLES.get(bundle_id)
    if not bundle: return jsonify({"error": "Invalid Bundle"}), 404
    
    service = get_gdrive_service()
    if not service: return jsonify({"error": "Service Unavailable"}), 500
    
    query = f"'{bundle['folder_id']}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    return jsonify({"files": results.get('files', [])})

# --- CHECK ACCESS ---
@app.route('/check_access', methods=['POST'])
def check_access():
    if access_collection is None:
        return jsonify({'status': 'expired'})

    data = request.json
    email = data.get('email', '').lower().strip()
    bundle_id = data.get('bundle_id')

    user = access_collection.find_one({
        "email": email,
        "bundle_id": bundle_id
    })

    if user:
        return jsonify({
            'status': 'active', 
            'token': user['token'],
            'message': f"Welcome back! Access active for {bundle_id.upper()}."
        })
    else:
        return jsonify({'status': 'expired'})

# --- REDEEM COUPON (Checks Validity Only) ---
@app.route('/redeem_coupon', methods=['POST'])
def redeem_coupon():
    data = request.json
    code = data.get('coupon_code', '').strip().upper()
    email = data.get('email')
    bundle_id = data.get('bundle_id')

    if code in COUPONS:
        discount = COUPONS[code]
        
        # Scenario A: 100% Off (Free Access)
        if discount == 100:
            token = create_access_token(bundle_id)
            if access_collection is not None:
                access_collection.insert_one({
                    "email": email,
                    "bundle_id": bundle_id,
                    "token": token,
                    "created_at": datetime.utcnow(),
                    "payment_method": "COUPON",
                    "coupon_used": code
                })
            return jsonify({
                'status': 'success', 
                'token': token, 
                'message': 'Coupon Applied! Free Access Granted.'
            })
            
        # Scenario B: Partial Discount (e.g., 10% Off)
        else:
            return jsonify({
                'status': 'partial', 
                'message': f'✅ Coupon Valid! {discount}% Discount Applied.'
            })

    return jsonify({'status': 'invalid', 'message': 'Invalid or Expired Coupon.'})

# --- CREATE ORDER (Calculates Price for Razorpay) ---
@app.route('/create_order', methods=['POST'])
def create_order():
    data = request.json
    bundle_id = data.get('bundle_id')
    user_email = data.get('email', '').lower().strip()
    coupon_code = data.get('coupon_code', '').upper().strip()
    
    bundle = BUNDLES.get(bundle_id)
    if not bundle: return jsonify({'error': 'Invalid Bundle'}), 400

    final_price = bundle['price']
    
    # 1. Check Loyalty (50% Off)
    if loyalty_collection is not None:
        is_loyal = loyalty_collection.find_one({"email": user_email})
        if is_loyal:
            final_price = int(final_price * 0.5) 
            print(f"🎉 Loyalty Discount Applied for {user_email}")

    # 2. Check Coupon (Partial Discount)
    # Note: If user is loyal (50% off) AND uses a 10% coupon, 
    # the 10% is applied to the already discounted price.
    if coupon_code in COUPONS:
        discount_percent = COUPONS[coupon_code]
        if discount_percent < 100:
            multiplier = (100 - discount_percent) / 100
            final_price = int(final_price * multiplier)
            print(f"🎟️ Coupon {coupon_code} applied: {discount_percent}% Off")

    # Create Razorpay Order
    client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_SECRET))
    order = client.order.create({
        'amount': final_price,
        'currency': 'INR',
        'payment_capture': '1'
    })
    
    return jsonify({
        'order_id': order['id'], 
        'key_id': RAZORPAY_KEY_ID, 
        'amount': final_price
    })

@app.route('/verify_payment', methods=['POST'])
def verify_payment():
    data = request.json
    try:
        client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_SECRET))
        client.utility.verify_payment_signature({
            'razorpay_order_id': data['razorpay_order_id'],
            'razorpay_payment_id': data['razorpay_payment_id'],
            'razorpay_signature': data['razorpay_signature']
        })
        
        token = create_access_token(data['bundle_id'])
        
        if access_collection is not None:
            access_collection.insert_one({
                "email": data.get('user_email'),
                "bundle_id": data['bundle_id'],
                "token": token,
                "created_at": datetime.utcnow()
            })
        
        return jsonify({'status': 'success', 'token': token})

    except Exception as e:
        print(f"Payment Error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/download/<file_id>')
def download_file(file_id):
    token = request.args.get('token')
    if not token or not verify_token(token):
        return "❌ ACCESS DENIED: Link Expired.", 403

    service = get_gdrive_service()
    try:
        meta = service.files().get(fileId=file_id).execute()
        file_name = meta.get('name', 'download.pdf')
        
        drive_request = service.files().get_media(fileId=file_id)
        file_stream = io.BytesIO()
        downloader = MediaIoBaseDownload(file_stream, drive_request)
        
        done = False
        while not done: _, done = downloader.next_chunk()
        file_stream.seek(0)
        
        return send_file(file_stream, as_attachment=True, download_name=file_name)
    except Exception as e:
        return f"Error: {e}", 500
    
@app.route('/about')
def about():
    return render_template('about.html')

# --- SECRET ROUTE: ADD LOYAL USER ---
# Visit: /add_loyal?email=friend@gmail.com&pw=1234
@app.route('/add_loyal')
def add_loyal():
    if loyalty_collection is None: return "DB Error"
    
    email = request.args.get('email')
    password = request.args.get('pw')
    
    if not email or password != '1234': 
        return "❌ Access Denied"
    
    loyalty_collection.update_one(
        {"email": email.lower().strip()}, 
        {"$set": {"email": email.lower().strip()}}, 
        upsert=True
    )
    return f"✅ Added {email} to Loyalty List! They will get 50% off."

if __name__ == '__main__':
    app.run(debug=True)
