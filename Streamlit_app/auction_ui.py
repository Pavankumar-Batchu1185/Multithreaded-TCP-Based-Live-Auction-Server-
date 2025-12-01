import streamlit as st
import subprocess
from pathlib import Path
import psutil
import mysql.connector
import hashlib
from datetime import datetime
from datetime import timezone
import time
from auction_listener import finalize_mongo_auction
from auction_listener import get_product_from_mongo, save_product_to_mongo, products_col
from auction_listener import add_to_waiting_room, remove_from_waiting_room, get_waiting_users, clear_waiting_room,get_seller_stats,get_buyer_stats
from auction_listener import get_closed_auctions
from bson import ObjectId
import socket
import random
import string
import os
import queue
from streamlit_autorefresh import st_autorefresh
from email_sender import notify_buyers
from urllib.parse import quote_plus, quote
from pymongo import MongoClient
from bson.decimal128 import Decimal128

DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "123456",
    "database": "auction_system"
}

SERVER_EXE = Path(r"D:\TY SEM1\CN\CP\Multithreaded-TCP-Based-Live-Auction-Server-\Server\AuctionServer.exe")
SERVER_HOST = "127.0.0.1" 
SERVER_PORT = 8000

def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG)

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

import re

def is_valid_email(email):
    if not email:
        return False
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def user_exists(username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username=%s", (username,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def validate_user(username, password):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users WHERE username=%s", (username,))
    user = cursor.fetchone()
    conn.close()
    if user and user.get("password_hash") == hash_password(password):
        return user
    return None

def register_user(username, password, role, email=None):
    if user_exists(username):
        st.error("Username already exists.")
        return False
    if not email or not email.strip():
        st.error("❌ Email address is required.")
        return False
    
    final_email = email.strip()
    
    if not is_valid_email(final_email):
        st.error("❌ Please provide a valid email address (e.g., user@example.com).")
        return False
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO users (username, password_hash, role, email) VALUES (%s, %s, %s, %s)",
        (username, hash_password(password), role, final_email)
    )
    conn.commit()
    conn.close()
    st.success("✅ Registration successful. Please login.")
    return True

def is_server_running():
    for process in psutil.process_iter(['pid', 'name']):
        try:
            if process.info['name'] and "AuctionServer.exe" in process.info['name']:
                return True
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return False

def kill_server():
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if proc.info['name'] and "AuctionServer.exe" in proc.info['name']:
                proc.kill()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    st.success("Auction Server stopped successfully.")

def generate_auction_code():
    return "AUC-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))

def insert_auction(product_id: str, product_name: str, base_price: float, duration_minutes: int = 2):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute("""
        SELECT * FROM auctions 
        WHERE product_id=%s AND status='active'
    """, (product_id,))
    
    existing = cursor.fetchone()
    if existing:
        conn.close()
        raise ValueError(f"Product already has an active auction (Code: {existing.get('auction_code')})")
    
    try:
        product = products_col.find_one({"_id": ObjectId(product_id)})
        if product and product.get("status") != "available":
            conn.close()
            raise ValueError(f"Product is not available (Status: {product.get('status')})")
    except Exception as e:
        conn.close()
        raise ValueError(f"Could not verify product availability: {e}")
    
    auction_code = generate_auction_code()
    cursor.execute(
        """INSERT INTO auctions
           (product_id, product_name, base_price, status, start_time,
            duration_minutes, created_by, auction_code)
           VALUES (%s,%s,%s,'active',UTC_TIMESTAMP() , %s, %s, %s)""",
        (product_id, product_name[:255], float(base_price),
         int(duration_minutes), st.session_state.username, auction_code)
    )
    auction_id = cursor.lastrowid
    conn.commit()
    conn.close()
    st.session_state.last_auction_code = auction_code
    try:
        products_col.update_one(
            {"_id": ObjectId(product_id)},
            {"$set": {
                "status": "in_auction",
                "auction_code": auction_code,
                "auction_id": auction_id
            }}
        )
    except Exception as e:
        st.warning(f"Could not update product status in MongoDB: {e}")

    return auction_code, auction_id

def close_expired_auctions():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM auctions WHERE status='active'")
    active_auctions = cursor.fetchall()

    for auction in active_auctions:
        start = auction.get("start_time")
        duration = auction.get("duration_minutes") or 0

        if not start:
            continue

        if isinstance(start, str):
            start = datetime.fromisoformat(start)
        start_utc = start.replace(tzinfo=timezone.utc)
        now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
        elapsed = (now_utc - start_utc).total_seconds()
        if elapsed >= duration * 60:
            final_bid = float(auction.get("current_bid") or auction.get("base_price"))
            winner = auction.get("current_bidder", "No Bids")
            cursor.execute("""
                UPDATE auctions 
                SET status='closed', end_time=%s, final_bid=%s, winner=%s
                WHERE id=%s
            """, (datetime.utcnow(), final_bid, winner, auction["id"]))

            print(f"Closed Auction {auction['id']} | Winner: {winner} | Final Bid: {final_bid}")
            try:
                finalize_mongo_auction(auction["product_id"], winner, float(final_bid))
                products_col.update_one(
                    {"_id": ObjectId(auction["product_id"])},
                    {"$set": {
                        "status": "sold",
                        "sold_to": winner,
                        "sold_price": float(final_bid),
                        "sold_at": datetime.utcnow()
                    }}
                )
            except Exception as e:
                print("Error finalizing Mongo auction:", e)
            cursor.execute("DELETE FROM auctions WHERE id=%s", (auction["id"],))
            conn.commit()
            try:
                clear_waiting_room(auction.get("auction_code"))
            except:
                pass

    conn.close()

def get_active_auctions():
    close_expired_auctions()
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM auctions WHERE status='active'")
    rows = cursor.fetchall()
    conn.close()
    return rows or []

def get_seller_auctions(username):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT * FROM auctions 
        WHERE created_by = %s 
        ORDER BY start_time DESC
    """, (username,))
    rows = cursor.fetchall()
    conn.close()
    return rows or []

def get_auction_by_id(auction_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM auctions WHERE id=%s", (auction_id,))
    row = cursor.fetchone()
    conn.close()
    return row

class TCPClient:
    def __init__(self):
        self.socket = None
        self.connected = False
        self.message_queue = queue.Queue()
        self.error = None
    
    def connect(self, username: str, auction_code: str):
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(5)
            self.socket.connect((SERVER_HOST, SERVER_PORT))
            self.socket.settimeout(None)
            join_msg = f"{username}|{auction_code}|JOIN\n"
            self.socket.sendall(join_msg.encode())
            self.connected = True
            self.error = None
            return True, None
        except Exception as e:
            self.connected = False
            self.error = str(e)
            if self.socket:
                self.socket.close()
            return False, str(e)
    
    def send_bid(self, bid_amount: float, username: str, auction_code: str):
        if not self.connected or not self.socket:
            return False, "Not connected to server"
        try:
            bid_msg = f"{username}|{auction_code}|{bid_amount}\n"
            self.socket.sendall(bid_msg.encode())
            return True, None
        except Exception as e:
            self.connected = False
            return False, str(e)
    
    def disconnect(self):
        if self.socket:
            try:
                self.socket.sendall(b"LEAVE\n")
                self.socket.close()
            except:
                pass
        self.connected = False
        self.socket = None

def init_tcp_client():
    if 'tcp_client' not in st.session_state:
        st.session_state.tcp_client = TCPClient()

def cleanup_tcp_client():
    if 'tcp_client' in st.session_state:
        st.session_state.tcp_client.disconnect()
        del st.session_state.tcp_client

AVATAR_COLORS = [
    "#1abc9c","#2ecc71","#3498db","#9b59b6","#34495e","#f39c12","#e67e22","#e74c3c","#7f8c8d"
]

def _color_for_username(username: str):
    if not username:
        return AVATAR_COLORS[0]
    idx = sum(ord(c) for c in username) % len(AVATAR_COLORS)
    return AVATAR_COLORS[idx]

def svg_avatar_data_uri(username: str, size=64):
    initial = (username[0].upper() if username else "?")
    color = _color_for_username(username)
    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' width='{size}' height='{size}' viewBox='0 0 {size} {size}'>
      <rect rx='{size//2}' width='{size}' height='{size}' fill='{color}'/>
      <text x='50%' y='50%' font-size='{int(size*0.45)}' text-anchor='middle' fill='white' dy='.35em' font-family='Arial, Helvetica, sans-serif'>{initial}</text>
    </svg>"""
    return "data:image/svg+xml;utf8," + quote(svg, safe='')

def load_css_file(filename="index.css"):
    css_filepath = Path(__file__).parent / filename
    
    if not css_filepath.exists():
        st.error(f"CSS file not found at: {css_filepath}") 
        return ""
    
    try:
        with open(css_filepath, "r") as f:
            css_content = f.read()
            return f"<style>{css_content}</style>"
    except Exception as e:
        st.error(f"Error reading CSS file: {e}")
        return ""

def get_all_users():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, username, role, email, created_at FROM users ORDER BY created_at DESC")
    users = cursor.fetchall()
    conn.close()
    return users

def delete_user(user_id):
    """Delete a user by ID"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id=%s", (user_id,))
    conn.commit()
    conn.close()

def display_product_image_with_zoom(product_id, caption="Product Image", thumbnail_width=150):
    try:
        product, image_bytes = get_product_from_mongo(product_id)
        
        if image_bytes:
            col_thumb, col_zoom = st.columns([3, 1])
            
            with col_thumb:
                st.image(image_bytes, caption=caption, width=thumbnail_width)
            
            with col_zoom:
                zoom_key = f"zoom_modal_{product_id}"
                
                if st.button("🔍 Zoom", key=f"btn_{product_id}", use_container_width=True):
                    st.session_state[zoom_key] = not st.session_state.get(zoom_key, False)
            
            if st.session_state.get(zoom_key, False):
                with st.expander("🖼️ Full Size Image", expanded=True):
                    st.image(image_bytes, use_container_width=True)
                    
                    if st.button("❌ Close", key=f"close_{product_id}", use_container_width=True):
                        st.session_state[zoom_key] = False
                        st.rerun()
        else:
            st.image(
                "https://via.placeholder.com/300x225.png?text=No+Image+Available", 
                caption=caption,
                width=thumbnail_width
            )
        
        return product, image_bytes
        
    except Exception as e:
        st.error(f"Error loading product image: {e}")
        st.image(
            "https://via.placeholder.com/300x225.png?text=Error+Loading+Image", 
            caption="Error",
            width=thumbnail_width
        )
        return None, None

def admin_add_user(username, password, role, email=None):
    if user_exists(username):
        return False, "Username already exists."
    
    if not email or not email.strip():
        return False, "Email address is required."
    
    final_email = email.strip()
    
    if "@" not in final_email or "." not in final_email:
        return False, "Invalid email format."
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO users (username, password_hash, role, email) VALUES (%s, %s, %s, %s)",
        (username, hash_password(password), role, final_email)
    )
    conn.commit()
    conn.close()
    return True, "User added successfully!"

custom_css = load_css_file("index.css")
st.set_page_config(page_title="BidVerse", layout="wide", initial_sidebar_state="expanded", menu_items={'About': 'A multi-threaded TCP-based live auction system UI.'})

st.markdown(custom_css, unsafe_allow_html=True)

header_html = """
<div style="text-align: center; padding: 2rem 0; margin-bottom: 2rem;">
    <h1 style="
        font-size: 2.5rem;
        font-weight: 800;
        color: #1f2937;
        margin: 0;
    ">
        🚀 BidVerse
    </h1>
    <p style="
        color: #4b5563;
        font-size: 1.1rem;
        margin-top: 0.5rem;
        font-weight: 400;
    ">
        Real-time bidding • Secure transactions • Live updates
    </p>
</div>
"""
st.markdown(header_html, unsafe_allow_html=True)

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.role = None
    st.session_state.username = None
    st.session_state.selected_auction = None
    st.session_state.in_auction_room = False
    st.session_state.cached_auction = None
    st.session_state.last_bid_time = 0

# LOGIN / REGISTER
if not st.session_state.logged_in:
    col_login_spacer, col_login, col_reg, col_login_spacer_end = st.columns([1, 2, 2, 1])
    
    with col_login:
        login_card = """
         <div style="
            background: #ffffff;
            border-radius: 12px;
            padding: 2rem;
            border: 1px solid #e5e7eb;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
        ">
            <div style="text-align: center; margin-bottom: 1.5rem;">
                <div style="
                    width: 60px;
                    height: 60px;
                    background: #6366f1;
                    border-radius: 50%;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    font-size: 2rem;
                    margin-bottom: 1rem;
                ">🔑</div>
                <h2 style="
                    margin: 0;
                    color: #1f2937;
                    font-size: 1.75rem;
                    font-weight: 700;
                ">Login</h2>
                <p style="color: #6b7280; margin-top: 0.5rem;">Access your auction dashboard</p>
            </div>
        </div>
        """
        st.markdown(login_card, unsafe_allow_html=True)
        username = st.text_input("Username", key="login_user", placeholder="Enter your username")
        password = st.text_input("Password", type="password", key="login_pass", placeholder="Enter your password")
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("Login", use_container_width=True, type="primary"):
            user = validate_user(username, password)
            if user:
                st.session_state.logged_in = True
                st.session_state.role = user["role"]
                st.session_state.username = user["username"]
                st.toast(f"Welcome {user['username']} ({user['role']})!") # Use toast instead of st.success
                st.rerun()
            else:
                st.error("Invalid username or password.")
    
    with col_reg:
        register_card = """
        <div style="
            background: #ffffff;
            border-radius: 12px;
            padding: 2rem;
            border: 1px solid #e5e7eb;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
        ">
            <div style="text-align: center; margin-bottom: 1.5rem;">
                <div style="
                    width: 60px;
                    height: 60px;
                    background: #3b82f6;
                    border-radius: 50%;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    font-size: 2rem;
                    margin-bottom: 1rem;
                ">📝</div>
                <h2 style="
                    margin: 0;
                    color: #1f2937;
                    font-size: 1.75rem;
                    font-weight: 700;
                ">Register</h2>
                <p style="color: #6b7280; margin-top: 0.5rem;">Join as Buyer, Seller, or Admin</p>
            </div>
        </div>
        """
        st.markdown(register_card, unsafe_allow_html=True)
        new_user = st.text_input("Username", key="reg_user", placeholder="Choose a username")
        new_pass = st.text_input("Password", type="password", key="reg_pass", placeholder="Choose a strong password")
        new_email = st.text_input(
        "Email *", 
        key="reg_email", 
        placeholder="your.email@example.com",
        help="Required for all notifications"
        )
        role = st.selectbox("Role", ["Admin", "Seller", "Buyer"], index=2)
        if st.button("Register", use_container_width=True):
            # ✅ Validate all required fields
            if not new_user or not new_pass or not new_email:
                st.error("❌ Please fill all required fields (Username, Password, Email).")
            elif "@" not in new_email or "." not in new_email:
                st.error("❌ Please provide a valid email address.")
            else:
                if register_user(new_user, new_pass, role, new_email):
                    pass
else:
    role = st.session_state.role
    username = st.session_state.username
    with st.sidebar:
        sidebar_header = f"""
        <div style="
            background: #6366f1;
            padding: 1.5rem;
            border-radius: 12px;
            margin-bottom: 1.5rem;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            text-align: center;
        ">
            <div style="
                width: 80px;
                height: 80px;
                background: rgba(255, 255, 255, 0.2);
                border-radius: 50%;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                font-size: 2.5rem;
                margin-bottom: 1rem;
                border: 2px solid rgba(255, 255, 255, 0.3);
            ">👤</div>
            <h3 style="
                color: white;
                margin: 0;
                font-size: 1.5rem;
                font-weight: 700;
            ">{username}</h3>
            <div style="
                display: inline-block;
                padding: 6px 16px;
                background: rgba(255, 255, 255, 0.2);
                border-radius: 20px;
                margin-top: 0.5rem;
                color: white;
                font-weight: 600;
                font-size: 0.9rem;
            ">{role}</div>
        </div>
        """
        st.markdown(sidebar_header, unsafe_allow_html=True)
        # Define navigation options based on role
        nav_options = {
            "Admin": {
                "Server Control": "⚙️ Server Control", 
                "User Management": "👥 User Management",
                "Live Auctions": "🔴 Live Auctions",
                "Closed Auctions": "🔒 Closed Auctions",
                "Bid History": "📜 Bid History"
            },
            "Seller": {
                "Dashboard": "📊 Dashboard",          
                "Products": "📦 Product Catalog", 
                "My Auctions": "🔨 My Active Auctions"
            },
            "Buyer": {
                "Dashboard": "📊 Dashboard",          
                "Active Auctions": "💰 Live Auctions",
                "My Purchases": "🛍️ My Purchases",    
                "Bidding History": "📜 Bid History"   
            }
        }
        role_pages = nav_options.get(role, {})
        page_keys = list(role_pages.keys())
        
        st.header('📍 Navigation')

        page = st.radio(
            "Navigation:",  
            options=page_keys,
            format_func=lambda x: role_pages[x],
            index=page_keys.index(st.session_state.get('current_page', page_keys[0])),
            key='current_page',
            label_visibility="collapsed"  
        )
                
        st.markdown("<br><br><br>", unsafe_allow_html=True) 
        if st.button("🚪 Logout", use_container_width=True, type="secondary"):
            cleanup_tcp_client()
            st.session_state.clear()
            st.toast("Logged out successfully!")
            time.sleep(1)
            st.rerun()

    if role == "Seller" and page == "Products":
        st.header("📦 Product Catalog")
        st.subheader("⬆️ Upload New Product", help="Add a product to your catalog before starting an auction.")
        with st.expander("Click to add a new product", expanded=False):
            with st.form("upload_form", clear_on_submit=True):
                col_name, col_price = st.columns([3, 1])
                with col_name:
                    product_name = st.text_input("Product Name *", placeholder="Vintage Watch")
                with col_price:
                    base_price = st.number_input("Base Price *", min_value=1.0, step=1.0, format="%.2f")
                
                product_desc = st.text_area("Product Description", placeholder="Brief description of the item...")
                image_file = st.file_uploader("Upload Product Image *", type=["png", "jpg", "jpeg"])
                
                st.markdown("<br>", unsafe_allow_html=True)
                submitted = st.form_submit_button("💾 Save Product to Catalog", type="primary")
                
                if submitted:
                    if not product_name or not image_file:
                        st.error("Product name and image are required.")
                    else:
                        image_bytes = image_file.read()
                        try:
                            pid = save_product_to_mongo(
                                seller=username,
                                name=product_name,
                                description=product_desc,
                                base_price=base_price,
                                image_bytes=image_bytes
                            )
                            st.success(f"Product saved successfully! ID: {pid}")
                        except Exception as e:
                            st.error(f"Could not save product: {e}")
                        st.rerun()

        st.markdown("---")
        st.header("📋 Your Products Overview")
        available_products = list(products_col.find({"seller": username, "status": "available"}))
        in_auction_products = list(products_col.find({"seller": username, "status": "in_auction"}))
        sold_products = list(products_col.find({"seller": username, "status": "sold"}))
        tab_available, tab_in_auction, tab_sold = st.tabs([
            f"Available ({len(available_products)})",
            f"In Auction ({len(in_auction_products)})",
            f"Sold ({len(sold_products)})"
        ])
        st.subheader("✅ Available for Auction")
        if not available_products:
            st.info("No available products. Please upload one above.")
        else:
            cols = st.columns(3, gap="medium") 
            for i, p in enumerate(available_products):
                with cols[i % 3]: 
                    with st.container(border=True):
                        st.markdown(f"### {p.get('name')}", unsafe_allow_html=True)
                        display_product_image_with_zoom(
                            str(p["_id"]), 
                            caption=p.get('name'), 
                            thumbnail_width=200
                        )

                        st.markdown(f"<p style='font-size: 1.2rem;'>*Base Price:* **<span style='color: #f39c12;'>${p.get('base_price', 'N/A')}</span>**</p>", unsafe_allow_html=True)
                        st.write(f"Description: {p.get('description', '')[:50]}...")
                        with st.form(f"start_auction_{p['_id']}"):
                            duration_key = f"dur_{p['_id']}"
                            meet_key = f"meet_{p['_id']}"
                            
                            duration = st.number_input("Duration (minutes)", min_value=1, max_value=60, value=2, key=duration_key)
                            meet_link = st.text_input("Google Meet Link", key=meet_key, placeholder="https://meet.google.com/xxxx-xxxx-xxx")

                            if st.form_submit_button(f"🔨 Start Live Auction", type="primary", use_container_width=True):
                                if not meet_link:
                                    st.error("Please provide a Google Meet link to invite buyers.")
                                else:
                                    try:
                                        auction_code, auction_id = insert_auction(
                                            product_id=str(p["_id"]),
                                            product_name=p.get("name"),
                                            base_price=p.get("base_price", 0),
                                            duration_minutes=int(duration)
                                        )
                                        start_time = datetime.utcnow().isoformat()
                                        try:
                                            notify_buyers(
                                                product_name=p.get("name"),
                                                auction_code=auction_code,
                                                start_time=start_time,
                                                duration_minutes=int(duration),
                                                meet_link=meet_link,
                                                base_price=p.get("base_price", 0)
                                            )
                                            st.success(f"Auction started! Code: **{auction_code}** — buyers notified.")
                                        except Exception as e:
                                            st.warning(f"Auction started (code {auction_code}) but notify thread failed: {e}")
                                    except Exception as e:
                                        st.error(f"Failed to start auction: {e}")
                                    st.rerun()

        with tab_in_auction:
            if not in_auction_products:
                st.info("No products currently in auction.")
            else:
                for p in in_auction_products:
                    st.warning(f"🟠 **{p.get('name')}** — Auction running (Code: `{p.get('auction_code','N/A')}`). View details in the 'My Auctions' tab.")
        
        with tab_sold:
            if not sold_products:
                st.info("No sold products yet.")
            else:
                for p in sold_products:
                    st.success(f"🎉 **{p.get('name')}** sold to **{p.get('sold_to','N/A')}** for **${p.get('sold_price','N/A')}**")
                    st.caption(f"Code: {p.get('auction_code', 'N/A')} | Sold At: {p.get('sold_at')}")

    # Admin UI: Server Control
    elif role == "Admin" and page == "Server Control":
        st.header("⚙️ Auction Server Control Panel")
        
        server_status_html = """
        <div style="
            background: #ffffff;
            border-radius: 12px;
            padding: 2rem;
            border: 1px solid #e5e7eb;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            margin-bottom: 2rem;
        ">
        """
        st.markdown(server_status_html, unsafe_allow_html=True)
        
        col_status, col_button = st.columns([3, 1])
        server_running = is_server_running()

        if server_running:
            status_card = """
             <div style="
                background: #10b981;
                padding: 1.5rem;
                border-radius: 12px;
                color: white;
                box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            ">
                <div style="display: flex; align-items: center; gap: 1rem;">
                    <div style="
                        width: 50px;
                        height: 50px;
                        background: rgba(255, 255, 255, 0.2);
                        border-radius: 50%;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        font-size: 1.5rem;
                    ">✅</div>
                    <div>
                        <div style="font-size: 0.9rem; opacity: 0.9;">Server Status</div>
                        <div style="font-size: 1.8rem; font-weight: 700;">ACTIVE</div>
                        <div style="font-size: 0.85rem; opacity: 0.9;">Running smoothly</div>
                    </div>
                </div>
            </div>
            """
            col_status.markdown(status_card, unsafe_allow_html=True)
            with col_button:
                if st.button("🛑 Stop Server", type="secondary", use_container_width=True):
                    kill_server()
                    st.rerun()
        else:
            status_card = """
           <div style="
                background: #ef4444;
                padding: 1.5rem;
                border-radius: 12px;
                color: white;
                box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            ">
                <div style="display: flex; align-items: center; gap: 1rem;">
                    <div style="
                        width: 50px;
                        height: 50px;
                        background: rgba(255, 255, 255, 0.2);
                        border-radius: 50%;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        font-size: 1.5rem;
                    ">⏸️</div>
                    <div>
                        <div style="font-size: 0.9rem; opacity: 0.9;">Server Status</div>
                        <div style="font-size: 1.8rem; font-weight: 700;">INACTIVE</div>
                        <div style="font-size: 0.85rem; opacity: 0.9;">Not Running</div>
                    </div>
                </div>
            </div>
            """
            col_status.markdown(status_card, unsafe_allow_html=True)
            with col_button:
                if st.button("🚀 Start Server", type="primary", use_container_width=True):
                    if not SERVER_EXE.exists():
                        st.error(f"Server executable not found: `{SERVER_EXE}`")
                    else:
                        try:
                            subprocess.Popen([str(SERVER_EXE)], cwd=str(SERVER_EXE.parent),
                                               creationflags=subprocess.CREATE_NEW_CONSOLE)
                            st.success("Auction Server launched successfully.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to start server: {e}")
        
        st.markdown("</div>", unsafe_allow_html=True)

    #  Admin UI: Closed Auctions
    elif role == "Admin" and page == "Closed Auctions":
        st.header("🔒 Closed Auctions Archive")
        closed_auctions = get_closed_auctions()
        if not closed_auctions:
            st.info("No closed auctions yet.")
        else:
           cols = st.columns(2, gap="large") 
           for i, a in enumerate(closed_auctions):
                with cols[i % 2]:
                    with st.container(border=True):
                        prod, image_bytes = (None, None)
                        try:
                            prod, image_bytes = get_product_from_mongo(a.get("product_id"))
                        except Exception:
                            pass
                        
                        col_img, col_info = st.columns([1, 2])
                        with col_img:
                            if image_bytes:
                                st.image(image_bytes, width=120)
                            else:
                                st.image("https://via.placeholder.com/100x75.png?text=Item", width=120)
                        
                        with col_info:
                            st.markdown(f"### {a.get('product_name')}", unsafe_allow_html=True)
                            st.metric("Final Bid", f"${a.get('final_bid') or a.get('base_price')}", help="The final price of the auction.")
                            st.caption(f"Winner: **{a.get('winner', 'N/A')}** | Code: `{a.get('auction_code')}`")
                        
                        with st.expander("Details"):
                            st.write(f"Created By: {a.get('created_by')}")
                            st.write(f"Ended: {a.get('end_time')}")
                            if prod and prod.get('description'):
                                st.caption(f"Description: {prod.get('description')}")

    # Admin UI: Live Auctions Monitor
    elif role == "Admin" and page == "Live Auctions":
        st_autorefresh(interval=5000, key="admin_live_refresh")
        
        st.header("🔴 Live Auctions Monitor")
        
        active_auctions = get_active_auctions()
        
        if not active_auctions:
            st.info("No active auctions at the moment.")
        else:
            st.success(f"**{len(active_auctions)}** active auction(s) running")
            
            for a in active_auctions:
                with st.container(border=True):
                    col_img, col_info, col_stats = st.columns([1, 2, 1])
                    with col_img:
                        try:
                            _, image_bytes = get_product_from_mongo(a.get("product_id"))
                            if image_bytes:
                                st.image(image_bytes, width=120)
                            else:
                                st.image("https://via.placeholder.com/120x90.png?text=Item", width=120)
                        except:
                            st.image("https://via.placeholder.com/120x90.png?text=Item", width=120)
                    with col_info:
                        st.markdown(f"### {a.get('product_name')}")
                        st.markdown(f"**Code:** `{a.get('auction_code')}`")
                        st.markdown(f"**Seller:** {a.get('created_by')}")
                        start = a.get("start_time")
                        duration = a.get("duration_minutes") or 0
                        if start:
                            if isinstance(start, str): 
                                start = datetime.fromisoformat(start)
                            start_utc = start.replace(tzinfo=timezone.utc)
                            now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
                            elapsed = (now_utc - start_utc).total_seconds()
                            remaining = max(0, duration * 60 - elapsed)
                            mins, secs = divmod(int(remaining), 60)
                            timer = f"{mins:02d}:{secs:02d}"
                            
                            if remaining < 60:
                                st.error(f"⏱️ Time Remaining: **{timer}**")
                            else:
                                st.info(f"⏱️ Time Remaining: **{timer}**")
                    with col_stats:
                        st.metric("Current Bid", f"${a.get('current_bid', a.get('base_price'))}")
                        st.metric("Base Price", f"${a.get('base_price')}")
                        st.caption(f"Highest Bidder: **{a.get('current_bidder', 'No bids yet')}**")
                    with st.expander(f"👥 Waiting Room ({a.get('auction_code')})"):
                        waiting_users = get_waiting_users(a.get('auction_code'))
                        if waiting_users:
                            st.write(f"**{len(waiting_users)} buyer(s) waiting:**")
                            cols = st.columns(4)
                            for idx, user_info in enumerate(waiting_users):
                                with cols[idx % 4]:
                                    st.caption(f"• {user_info['username']}")
                        else:
                            st.info("No buyers in waiting room")

  # Admin UI: User Management
    elif role == "Admin" and page == "User Management":
        st.header("👥 User Management")
        st.subheader("➕ Add New User")
        with st.expander("Add User Form", expanded=False):
            with st.form("admin_add_user_form", clear_on_submit=True):
                col1, col2 = st.columns(2)
                with col1:
                    new_username = st.text_input("Username *", placeholder="john_doe")
                    new_password = st.text_input("Password *", type="password", placeholder="Enter password")
                with col2:
                    new_role = st.selectbox("Role *", ["Buyer", "Seller", "Admin"])
                    new_email = st.text_input("Email *", placeholder="user@example.com")  # ✅ Required
                
                submitted = st.form_submit_button("➕ Add User", type="primary", use_container_width=True)
                
                if submitted:
                    if not new_username or not new_password or not new_email:
                        st.error("❌ All fields are required (Username, Password, Email, Role)!")
                    elif "@" not in new_email or "." not in new_email:
                        st.error("❌ Please provide a valid email address.")
                    else:
                        success, message = admin_add_user(new_username, new_password, new_role, new_email)
                        if success:
                            st.success(message)
                            time.sleep(1)
                            st.rerun()
                        else:
                            st.error(message)
        
        st.subheader("📋 All Users")
        users = get_all_users()
        
        if not users:
            st.info("No users found in the system.")
        else:
            search_term = st.text_input("🔍 Search users", placeholder="Search by username or role...")
            filtered_users = users
            if search_term:
                filtered_users = [u for u in users if 
                                search_term.lower() in u.get('username', '').lower() or 
                                search_term.lower() in u.get('role', '').lower()]
            
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total Users", len(users))
            col2.metric("Buyers", len([u for u in users if u.get('role') == 'Buyer']))
            col3.metric("Sellers", len([u for u in users if u.get('role') == 'Seller']))
            col4.metric("Admins", len([u for u in users if u.get('role') == 'Admin']))
            
            st.markdown("---")
            for user in filtered_users:
                with st.container(border=True):
                    col_avatar, col_info, col_actions = st.columns([1, 4, 1])
                    
                    with col_avatar:
                        avatar_url = svg_avatar_data_uri(user.get('username', 'U'), size=64)
                        st.markdown(f"""
                            <div style="text-align: center;">
                                <img src="{avatar_url}" style="border-radius: 50%; width: 64px; height: 64px; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
                            </div>
                        """, unsafe_allow_html=True)
                    
                    with col_info:
                        st.markdown(f"### {user.get('username')}")
                        role = user.get('role', 'Unknown')
                        role_colors = {
                            'Admin': '#ef4444',
                            'Seller': '#3b82f6',
                            'Buyer': '#10b981'
                        }
                        role_color = role_colors.get(role, '#6b7280')
                        
                        st.markdown(f"""
                            <span style="
                                display: inline-block;
                                padding: 4px 12px;
                                background: {role_color};
                                color: white;
                                border-radius: 12px;
                                font-size: 0.85rem;
                                font-weight: 600;
                            ">{role}</span>
                        """, unsafe_allow_html=True)
                        
                        st.caption(f"📧 Email: {user.get('email', 'N/A')}")
                        st.caption(f"📅 Created: {user.get('created_at', 'N/A')}")
                    
                    with col_actions:
                        if user.get('username') == username:
                            st.warning("You (current user)")
                        else:
                            delete_key = f"delete_confirm_{user.get('id')}"
                            if not st.session_state.get(delete_key, False):
                                if st.button("🗑️ Delete", key=f"del_{user.get('id')}", type="secondary", use_container_width=True):
                                    st.session_state[delete_key] = True
                                    st.rerun()
                            else:
                                st.error("Confirm?")
                                col_yes, col_no = st.columns(2)
                                with col_yes:
                                    if st.button("✅", key=f"yes_{user.get('id')}", use_container_width=True):
                                        try:
                                            delete_user(user.get('id'))
                                            st.success(f"User '{user.get('username')}' deleted!")
                                            st.session_state[delete_key] = False
                                            time.sleep(1)
                                            st.rerun()
                                        except Exception as e:
                                            st.error(f"Failed to delete: {e}")
                                with col_no:
                                    if st.button("❌", key=f"no_{user.get('id')}", use_container_width=True):
                                        st.session_state[delete_key] = False
                                        st.rerun()

    elif role == "Buyer" and page == "Active Auctions" and not st.session_state.get("in_auction_room", False):
        st_autorefresh(interval=10000, key="auction_list_refresh")
        header_html = """
        <div style="
            background: #6366f1;
            padding: 2rem;
            border-radius: 12px;
            margin-bottom: 2rem;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            text-align: center;
            color: white;
        ">
            <div style="font-size: 2.5rem; margin-bottom: 0.5rem;">💰</div>
            <h2 style="
                color: white;
                margin: 0;
                font-size: 2rem;
                font-weight: 700;
            ">Live Auctions</h2>
            <p style="margin: 0.5rem 0 0 0; opacity: 0.95;">Join active auctions and place your bids</p>
        </div>
        """
        st.markdown(header_html, unsafe_allow_html=True)
        join_card = """
           <div style="
            background: #ffffff;
            border-radius: 12px;
            padding: 2rem;
            border: 1px solid #e5e7eb;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            margin-bottom: 2rem;
        ">
            <div style="text-align: center; margin-bottom: 1.5rem;">
                <div style="
                    width: 60px;
                    height: 60px;
                    background: #f59e0b;
                    border-radius: 50%;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    font-size: 2rem;
                    margin-bottom: 1rem;
                ">🎯</div>
                <h3 style="
                    margin: 0;
                    color: #1f2937;
                    font-size: 1.5rem;
                    font-weight: 700;
                ">Join Auction via Code</h3>
            </div>
        </div>
        """
        st.markdown(join_card, unsafe_allow_html=True)
        col_code, col_button = st.columns([3, 1])
        code_input = col_code.text_input("Enter Auction Code (e.g., AUC-1A2B)", label_visibility="collapsed", placeholder="AUC-XXXX")
        if col_button.button("Join Now", use_container_width=True, type="primary"):
                conn = get_db_connection()
                cursor = conn.cursor(dictionary=True)
                cursor.execute("SELECT * FROM auctions WHERE auction_code=%s AND status='active'", (code_input,))
                auction = cursor.fetchone()
                cursor.close()
                conn.close()
                if auction:
                    st.session_state.selected_auction = auction["id"]
                    st.session_state.in_auction_room = True
                    st.rerun()
                else:
                    st.error("Invalid or closed auction code.")

        st.markdown("---")
        feed_header = """
        <div style="
            text-align: center;
            margin: 2rem 0;
            padding: 1rem;
        ">
            <h3 style="
                color: #1f2937;
                font-size: 1.75rem;
                font-weight: 700;
                margin: 0;
            ">🔥 Auction Feed</h3>
        </div>
        """
        st.markdown(feed_header, unsafe_allow_html=True)
        
        auctions = get_active_auctions()
        
        if not auctions:
            empty_state = """
        <div style="
                background: #ffffff;
                border-radius: 12px;
                padding: 4rem 2rem;
                text-align: center;
                border: 1px solid #e5e7eb;
                box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            ">
                <div style="font-size: 3rem; margin-bottom: 1rem;">🔍</div>
                <h3 style="
                    color: #4b5563;
                    font-size: 1.5rem;
                    font-weight: 700;
                    margin: 0;
                ">No Active Auctions</h3>
                <p style="color: #6b7280; margin-top: 0.5rem;">Check back later for new auctions</p>
            </div>
            """
            st.markdown(empty_state, unsafe_allow_html=True)
        else:
                cols = st.columns(2, gap="large") 
                for i, a in enumerate(auctions):
                    with cols[i % 2]: 
                        auction_card_html = f"""
                        <div style="
                            background: #ffffff;
                            border-radius: 12px;
                            padding: 1.5rem;
                            border: 1px solid #e5e7eb;
                            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
                            margin-bottom: 1.5rem;
                        " class="auction-card">
                        """
                        st.markdown(auction_card_html, unsafe_allow_html=True)
                        with st.container(border=False):
                            
                            start = a.get("start_time")
                            duration = a.get("duration_minutes") or 0
                            timer = "Unknown"
                            remaining = 0
                            if start:
                                if isinstance(start, str): start = datetime.fromisoformat(start)
                                start_utc = start.replace(tzinfo=timezone.utc)
                                now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
                                elapsed = (now_utc - start_utc).total_seconds()
                                remaining = max(0, duration * 60 - elapsed)
                                mins, secs = divmod(int(remaining), 60)
                                timer = f"{mins:02d}:{secs:02d}"
                            image_bytes = None
                            try:
                                _, image_bytes = get_product_from_mongo(a.get("product_id"))
                            except Exception:
                                pass
                            
                            col_img, col_info = st.columns([1, 2])
                            with col_img:
                                if image_bytes:
                                    st.image(image_bytes, width=100)
                                else:
                                    st.image("https://via.placeholder.com/100x75.png?text=Item", width=100)
                                
                            with col_info:
                                st.markdown(f"**{a.get('product_name')}** (`{a.get('auction_code')}`)")
                                timer_color = "#3498db"
                                if remaining < 60 and remaining > 0:
                                    timer_color = "#e74c3c"
                                elif remaining <= 0:
                                    timer_color = "#95a5a6"  
                                
                                st.markdown(f"<p style='margin: 0.5rem 0;'><strong>Time Remaining:</strong> <span style='color: {timer_color}; font-weight: bold; font-size: 1.1rem;'>{timer}</span></p>", unsafe_allow_html=True)
                                
                                st.metric("Current Bid", f"${a.get('current_bid', a.get('base_price'))}", help=f"Highest Bidder: {a.get('current_bidder', 'No bids yet')}")
                            st.markdown("---")
                            waiting_key = f"waiting_{a.get('auction_code')}"
                            joined = st.session_state.get(waiting_key, False)
                            
                            col_join, col_wait_btn = st.columns(2)
                            
                            with col_join:
                                if st.button(f"➡️ **Join Auction Room**", key=f"join_{a.get('id')}", type="primary", use_container_width=True):
                                    st.session_state.selected_auction = a.get('id')
                                    st.session_state.in_auction_room = True
                                    st.rerun()
                            
                            with col_wait_btn:
                                if not joined:
                                    if st.button("➕ Join Waitlist", key=f"join_wait_{a.get('auction_code')}", use_container_width=True, help="Join the pre-auction waiting list."):
                                        try:
                                            add_to_waiting_room(a.get("auction_code"), username)
                                            st.session_state[waiting_key] = True
                                            st.toast("Joined waiting room! Sellers will be notified.")
                                        except Exception as e:
                                            st.error(f"Could not join waiting room: {e}")
                                        st.rerun()
                                else:
                                    if st.button("Leave Waitlist", key=f"leave_wait_{a.get('auction_code')}", type="secondary", use_container_width=True):
                                        try:
                                            remove_from_waiting_room(a.get("auction_code"), username)
                                            st.session_state[waiting_key] = False
                                            st.info("You've left the waiting room.")
                                        except Exception as e:
                                            st.error(f"Could not leave waiting room: {e}")
                                        st.rerun()
                        
                            with st.expander(f"👥 View Waiting Room"):
                                waiting_users = get_waiting_users(a.get("auction_code"))
                                if not waiting_users:
                                    st.info("No one in the waiting room yet.")
                                else:
                                    st.write(f"**{len(waiting_users)} buyers waiting:**")
                                   
                                    wait_cols = st.columns(6)
                                    for idx, user_info in enumerate(waiting_users):
                                        col_idx = idx % 6
                                        with wait_cols[col_idx]:
                                            avatar_url = svg_avatar_data_uri(user_info["username"], size=36)
                                            st.markdown(f"""
                                                <div style="text-align: center; margin: 5px 0;">
                                                    <img src="{avatar_url}" style="border-radius: 50%; width: 36px; height: 36px;">
                                                    <p style="font-size: 10px; margin-top: 2px;">{user_info["username"]}</p>
                                                </div>
                                            """, unsafe_allow_html=True)
                        
                        st.markdown("</div>", unsafe_allow_html=True)

    elif role == "Buyer" and page == "Dashboard":
        st.header("📊 My Dashboard")
        stats = get_buyer_stats(username)
        st.subheader("📈 Overview")
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric("Auctions Participated", stats["total_participated"])
        with col2:
            st.metric("Auctions Won", stats["total_won"], 
                    delta=f"{stats['win_rate']:.1f}% Win Rate")
        with col3:
            st.metric("Total Spent", f"${stats['total_spent']:.2f}")
        with col4:
            st.metric("Avg Bid", f"${stats['avg_bid']:.2f}")
        
        st.markdown("---")
        
        st.subheader("🏆 Recent Wins")
        won_auctions = stats["won_auctions"][:5]  
        
        if not won_auctions:
            st.info("You haven't won any auctions yet. Keep bidding!")
        else:
            for auction in won_auctions:
                with st.container(border=True):
                    col_img, col_info = st.columns([1, 3])
                    
                    with col_img:
                        try:
                            _, image_bytes = get_product_from_mongo(auction.get("product_id"))
                            if image_bytes:
                                st.image(image_bytes, width=100)
                            else:
                                st.image("https://via.placeholder.com/100x75.png?text=Item", width=100)
                        except:
                            st.image("https://via.placeholder.com/100x75.png?text=Item", width=100)
                    
                    with col_info:
                        product_name = auction.get("product_name", "Unknown Product")
                        final_bid = auction.get("final_bid", 0)
                        if isinstance(final_bid, Decimal128):
                            final_bid = float(final_bid.to_decimal())
                        else:
                            final_bid = float(final_bid)
                        
                        closed_at = auction.get("closed_at", "N/A")
                        
                        st.markdown(f"### {product_name}")
                        st.success(f"**Won for: ${final_bid:.2f}**")
                        st.caption(f"Won on: {closed_at}")
        
        st.markdown("---")
        st.subheader("📊 Bidding Activity")
        if stats["total_participated"] > 0:
            st.write("**Win Rate**")
            st.progress(stats["win_rate"] / 100)
            st.caption(f"{stats['win_rate']:.1f}% of auctions won")
        else:
            st.info("No bidding activity yet. Start participating in auctions!")

    elif role == "Buyer" and page == "My Purchases":
        st.header("🛍️ My Purchases")
        
        stats = get_buyer_stats(username)
        won_auctions = stats["won_auctions"]
        
        if not won_auctions:
            st.info("You haven't purchased any items yet.")
        else:
            st.success(f"**Total Purchases: {len(won_auctions)}** | **Total Spent: ${stats['total_spent']:.2f}**")
            st.markdown("---")
            cols = st.columns(2, gap="large")
            for i, auction in enumerate(won_auctions):
                with cols[i % 2]:
                    with st.container(border=True):
                        col_img, col_details = st.columns([1, 2])
                        
                        with col_img:
                            try:
                                _, image_bytes = get_product_from_mongo(auction.get("product_id"))
                                if image_bytes:
                                    st.image(image_bytes, use_container_width=True)
                                else:
                                    st.image("https://via.placeholder.com/150x110.png?text=Product", use_container_width=True)
                            except:
                                st.image("https://via.placeholder.com/150x110.png?text=Product", use_container_width=True)
                        
                        with col_details:
                            product_name = auction.get("product_name", "Unknown")
                            final_bid = auction.get("final_bid", 0)
                            if isinstance(final_bid, Decimal128):
                                final_bid = float(final_bid.to_decimal())
                            else:
                                final_bid = float(final_bid)
                            
                            st.markdown(f"### {product_name}")
                            st.metric("Purchase Price", f"${final_bid:.2f}")
                            st.caption(f"Won: {auction.get('closed_at', 'N/A')}")
                            
                            num_bids = len(auction.get("bids", []))
                            st.caption(f"Total Bids: {num_bids}")

    elif role == "Buyer" and page == "Bidding History":
        st.header("📜 My Bidding History")
        
        stats = get_buyer_stats(username)
        participated = stats["participated_auctions"]
        
        if not participated:
            st.info("You haven't participated in any auctions yet.")
        else:
            # Stats overview
            col1, col2, col3 = st.columns(3)
            col1.metric("Total Participated", stats["total_participated"])
            col2.metric("Won", stats["total_won"])
            col3.metric("Lost", stats["total_participated"] - stats["total_won"])
            
            st.markdown("---")
            tab_all, tab_won, tab_lost = st.tabs(["All", "Won", "Lost"])
            
            with tab_all:
                for auction in participated:
                    with st.container(border=True):
                        product_name = auction.get("product_name", "Unknown")
                        winner = auction.get("winner", "No Bids")
                        final_bid = auction.get("final_bid", 0)
                        if isinstance(final_bid, Decimal128):
                            final_bid = float(final_bid.to_decimal())
                        else:
                            final_bid = float(final_bid)
                        
                        user_bids = [b for b in auction.get("bids", []) if b.get("bidder") == username]
                        highest_user_bid = max([float(b.get("amount", 0)) if not isinstance(b.get("amount"), Decimal128) 
                                            else float(b.get("amount").to_decimal()) for b in user_bids]) if user_bids else 0
                        
                        col_info, col_result = st.columns([3, 1])
                        
                        with col_info:
                            st.markdown(f"**{product_name}**")
                            st.write(f"Your Highest Bid: ${highest_user_bid:.2f}")
                            st.write(f"Winning Bid: ${final_bid:.2f} by {winner}")
                        
                        with col_result:
                            if winner == username:
                                st.success("🏆 WON")
                            else:
                                st.error("❌ LOST")
            
            with tab_won:
                won_list = [a for a in participated if a.get("winner") == username]
                if not won_list:
                    st.info("No wins yet.")
                else:
                    for auction in won_list:
                        st.success(f"🏆 {auction.get('product_name', 'Unknown')}")
            
            with tab_lost:
                lost_list = [a for a in participated if a.get("winner") != username]
                if not lost_list:
                    st.info("You've won all your auctions! 🎉")
                else:
                    for auction in lost_list:
                        st.error(f"❌ {auction.get('product_name', 'Unknown')}")

    #  Buyer UI: Inside Auction Room (Bidding)
    if role == "Buyer" and st.session_state.in_auction_room and st.session_state.selected_auction:
        st_autorefresh(interval=3000, key="auction_room_refresh")
        init_tcp_client()

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM auctions WHERE id=%s", (st.session_state.selected_auction,))
        auction = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if not auction or auction.get('status') == 'closed':
            if auction and auction.get('winner') == username:
                st.balloons()
                st.success(f"🎉 Congratulations! You won the auction for **{auction.get('product_name')}** with a final bid of **${auction.get('final_bid')}**!")
            else:
                st.warning(f"🔒 This auction for **{auction.get('product_name', 'product')}** has ended.")
                st.info(f"Final Bid: ${auction.get('final_bid', auction.get('base_price'))} | Winner: {auction.get('winner', 'No winner')}")
            
            if st.button("⬅️ Back to Live Auctions", type="primary"):
                cleanup_tcp_client()
                st.session_state.in_auction_room = False
                st.session_state.selected_auction = None
                st.session_state.cached_auction = None
                st.rerun()
            st.stop() 

        tcp_client = st.session_state.tcp_client
        if not tcp_client.connected:
            success, error = tcp_client.connect(username, auction.get("auction_code"))
            if not success:
                st.error(f"❌ Connection failed: {error}. Make sure the Auction Server is running.")
                col_retry, col_back = st.columns(2)
                if col_retry.button("🔄 Retry Connection", type="primary"):
                    st.rerun()
                if col_back.button("⬅️ Back to Active Auctions"):
                    cleanup_tcp_client()
                    st.session_state.in_auction_room = False
                    st.session_state.selected_auction = None
                    st.rerun()
                st.stop() 
                st.toast(f"✅ Connected to auction {auction.get('auction_code')}!")

        auction_header = f"""
     <div style="
            background: #6366f1;
            padding: 2rem;
            border-radius: 12px;
            margin-bottom: 2rem;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            text-align: center;
            color: white;
        ">
            <div style="font-size: 2.5rem; margin-bottom: 0.5rem;">🔨</div>
            <h2 style="
                color: white;
                margin: 0;
                font-size: 1.75rem;
                font-weight: 700;
            ">Auction Room</h2>
            <p style="
                margin: 0.5rem 0 0 0;
                font-size: 1.1rem;
                opacity: 0.95;
            ">{auction.get('product_name')}</p>
            <div style="
                display: inline-block;
                padding: 8px 20px;
                background: rgba(255, 255, 255, 0.2);
                border-radius: 20px;
                margin-top: 1rem;
                font-weight: 600;
            ">Code: {auction.get('auction_code')}</div>
        </div>
        """
        st.markdown(auction_header, unsafe_allow_html=True)
        
        current_bid = auction.get("current_bid") or auction.get("base_price")
        current_bidder = auction.get("current_bidder") or "No bids yet"
        
        col_m1, col_m2, col_m3 = st.columns(3)
        delta_text = ""
        delta_color = "off"
        if current_bidder == username:
            delta_text = "Winning"
            delta_color = "normal"
        elif current_bidder != "No bids yet":
            delta_text = "Outbid"
            delta_color = "inverse" 
        else:
            delta_text = "Start Bid"
            delta_color = "off"
        
  
        metric_style = """
        <style>
        [data-testid="stMetricValue"] {
            font-size: 2rem !important;
            font-weight: 900 !important;
        }
        [data-testid="stMetricLabel"] {
            font-size: 0.9rem !important;
            font-weight: 600 !important;
            opacity: 0.8;
        }
        </style>
        """
        st.markdown(metric_style, unsafe_allow_html=True)
        
        col_m1.metric("Current Highest Bid", f"${current_bid}", delta=delta_text, delta_color=delta_color)
        col_m2.metric("Highest Bidder", current_bidder, help="The user currently in the lead.")
        col_m3.metric("Base Price", f"${auction.get('base_price')}")
        
        st.markdown("---")
        
        col_image, col_timer_bid = st.columns([1, 2])
        with col_image:
         with st.container(border=True):
            image_bytes = None
            try:
                prod, image_bytes = get_product_from_mongo(auction.get("product_id"))
            except Exception:
                prod = None
                
            if image_bytes:
                st.image(image_bytes, caption=auction.get('product_name'), use_container_width=True)
            else:
                st.image("https://via.placeholder.com/250x200.png?text=Product+Image", use_container_width=True)
            
            if prod and prod.get('description'):
                with st.expander("Product Description"):
                    st.write(prod.get('description'))
        
        with col_timer_bid:
         with st.container(border=True):
            start_time = auction.get("start_time")
            duration = auction.get("duration_minutes") or 0
            
            remaining = 0
            timer_display = "N/A"
            if start_time:
                if isinstance(start_time, str): start_time = datetime.fromisoformat(start_time)
                start_utc = start_time.replace(tzinfo=timezone.utc)
                now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
                elapsed = (now_utc - start_utc).total_seconds()
                remaining = max(0, duration * 60 - elapsed)
                mins, secs = divmod(int(remaining), 60)
                timer_display = f"{mins:02d}:{secs:02d}"

                if remaining <= 0:
                    timer_html = f"""
                      <div style="
                        background: #ef4444;
                        padding: 2rem;
                        border-radius: 12px;
                        text-align: center;
                        color: white;
                        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
                    ">
                        <div style="font-size: 2.5rem; margin-bottom: 0.5rem;">🔒</div>
                        <h2 style="
                            color: white;
                            margin: 0;
                            font-size: 1.75rem;
                            font-weight: 700;
                        ">AUCTION ENDED</h2>
                    </div>
                    """
                    st.markdown(timer_html, unsafe_allow_html=True)
                elif remaining < 30:
                    timer_html = f"""
                  <div style="
                        background: #f59e0b;
                        padding: 2rem;
                        border-radius: 12px;
                        text-align: center;
                        color: white;
                        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
                    ">
                        <div style="font-size: 2.5rem; margin-bottom: 0.5rem;">🔥</div>
                        <h2 style="
                            color: white;
                            margin: 0;
                            font-size: 1.75rem;
                            font-weight: 700;
                        ">LAST CHANCE: {timer_display}</h2>
                    </div>
                    """
                    st.markdown(timer_html, unsafe_allow_html=True)
                else:
                    timer_html = f"""
                    <div style="
                        background: #3b82f6;
                        padding: 2rem;
                        border-radius: 12px;
                        text-align: center;
                        color: white;
                        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
                    ">
                        <div style="font-size: 2.5rem; margin-bottom: 0.5rem;">⏱️</div>
                        <div style="font-size: 0.9rem; opacity: 0.95; margin-bottom: 0.5rem;">Time Remaining</div>
                        <h2 style="
                            color: white;
                            margin: 0;
                            font-size: 2.25rem;
                            font-weight: 700;
                            font-family: 'Courier New', monospace;
                        ">{timer_display}</h2>
                    </div>
                    """
                    st.markdown(timer_html, unsafe_allow_html=True)

            st.markdown("---")
            st.subheader("Place Your Bid")
            min_bid = float(current_bid) + 1.0
            
            if remaining <= 0:
                    st.error("Bidding is closed as the auction has ended.")
            else:
             with st.form(key="bid_form", clear_on_submit=True):
                bid_value = st.number_input(
                    "Enter your bid amount",
                    min_value=min_bid,
                    value=min_bid,
                    format="%.2f",
                    help=f"Minimum next bid: ${min_bid:.2f}"
                )
                submitted = st.form_submit_button("🔨 Place Bid", type="primary", use_container_width=True)
                
                if submitted:
                    current_time = time.time()
                    if current_time - st.session_state.last_bid_time < 1:
                        st.error("🚫 Please wait a moment (1s cooldown) before placing another bid.")
                    else:
                        success, error = tcp_client.send_bid(bid_value, username, auction.get("auction_code"))
                        if success:
                            st.toast(f"✅ Bid of ${bid_value} sent!")
                            st.session_state.last_bid_time = current_time
                            time.sleep(0.5)
                            st.rerun()
                        else:
                            st.error(f"❌ Failed to send bid: {error}. Attempting to reconnect...")
                            cleanup_tcp_client()
                            st.rerun()
            
            st.markdown("---")
            col_leave, col_status = st.columns([2, 1])
            
            with col_leave:
                if st.button("⬅️ Leave Auction Room", use_container_width=True, type="secondary"):
                    cleanup_tcp_client()
                    st.session_state.in_auction_room = False
                    st.session_state.selected_auction = None
                    st.session_state.cached_auction = None
                    st.session_state.pop('current_page', None)  
                    st.success("Left auction room")
                    time.sleep(0.5)
                    st.rerun()
            
            with col_status:
                if tcp_client.connected:
                    st.markdown("🟢 **Connected**")
                else:
                    st.markdown("🔴 **Disconnected**")
          
    elif role == "Admin" and page == "Bid History":
        st.header("📜  Bid History ")
        try:
            client = MongoClient("mongodb+srv://pavankumarbatchu1185_db_user:Bvnspk%401185@cluster0.asbvkak.mongodb.net/")
            db = client["auction_data"]
            @st.cache_data(ttl=60) 
            def load_history():
                return list(db["auction_history"].find().sort("closed_at", -1))
            
            history = load_history()
            
            if not history:
                st.info("No completed auction history found.")
            else:
                for doc in history:
                    
                    with st.container(border=True):
                    
                        product_name = str(doc.get("product_name") or "N/A")
                        auction_code = str(doc.get("auction_code") or "N/A")

                        closed_at = doc.get("closed_at")
                        if hasattr(closed_at, "strftime"):
                            closed_at_str = closed_at.strftime("%Y-%m-%d %H:%M:%S")
                        else:
                            closed_at_str = str(closed_at) if closed_at else "N/A"

                       
                        final_bid_raw = doc.get("final_bid")
                        from bson.decimal128 import Decimal128
                        try:
                            if isinstance(final_bid_raw, Decimal128):
                                final_bid = f"{float(final_bid_raw.to_decimal()):.2f}"
                            else:
                                final_bid = f"{float(final_bid_raw):.2f}"
                        except:
                            final_bid = str(final_bid_raw)

                        winner = str(doc.get("winner") or "No Bids")

                       
                        st.markdown(f"""
                        <div class="bid-card">
                            <div class="bid-title">{product_name}</div>
                            <div class="bid-subinfo">
                                <b>Code:</b> {auction_code} &nbsp; | &nbsp;
                                <b>Ended:</b> {closed_at_str}
                            </div>
                            <div class="bid-winner-box">
                                Winner: <b>{winner}</b> &nbsp; | &nbsp;
                                Final Price: <span>${final_bid}</span>
                            </div>
                        </div>
                        """, unsafe_allow_html=True)

                        
                        
                        with st.expander("View All Bids"):
                            bids = doc.get("bids", [])
                            if not bids:
                                st.write("No individual bids recorded.")
                            else:
                                st.markdown("##### Bid Log (Newest First)")
                                st.table([
                                    {
                                        "Time (UTC)": bid.get('timestamp'),
                                        "Bidder": bid.get('bidder'),
                                        "Amount": f"${float(bid.get('amount').to_decimal()):.2f}" if isinstance(bid.get('amount'), Decimal128) else f"${bid.get('amount'):.2f}"
                                    }
                                    for bid in sorted(bids, key=lambda x: x.get("timestamp"), reverse=True)
                                ])
                   
        except Exception as e:
            st.error(f"Could not connect to MongoDB or load history: {e}")

    #  Seller UI: My Auctions
    elif role == "Seller" and page == "My Auctions":
        
        st.header("🔨 My Auctions Overview")
        my_auctions = get_seller_auctions(username)
        if not my_auctions:
            st.info("You haven't started any auctions yet.")
        else:
            active_auctions = [a for a in my_auctions if a.get('status') == 'active']
            closed_auctions = [a for a in my_auctions if a.get('status') == 'closed']
            
            tab_active, tab_closed = st.tabs([f"Active ({len(active_auctions)})", f"Closed History ({len(closed_auctions)})"])
            with tab_active:
                if not active_auctions:
                    st.info("No auctions are currently running.")
                else:
                    for a in active_auctions:
                        with st.container(border=True):
                            col_img, col_info, col_controls = st.columns([1, 2, 1])
                            
                          
                            with col_img:
                                image_bytes = None
                                try:
                                    _, image_bytes = get_product_from_mongo(a.get("product_id"))
                                except Exception:
                                    pass
                                if image_bytes:
                                    st.image(image_bytes, width=100)
                                else:
                                    st.image("https://via.placeholder.com/100x75.png?text=Item", width=100)
                            
                           
                            with col_info:
                                st.markdown(f"**{a.get('product_name')}** (`{a.get('auction_code')}`)")
                                st.markdown(f"Status: <span style='color: #2ecc71;'>**ACTIVE**</span>", unsafe_allow_html=True)
                                st.write(f"Base Price: **${a.get('base_price')}**")
                                st.metric("Current Bid", f"${a.get('current_bid', a.get('base_price'))}", help=f"Highest Bidder: {a.get('current_bidder', 'No bids yet')}")
                            
                           
                            with col_controls:
                               
                                with st.expander(f"👥 Waitlist"):
                                    waiting_users = get_waiting_users(a.get('auction_code'))
                                    if waiting_users:
                                        st.write(f"**{len(waiting_users)} buyers waiting**")
                                    
                                        for user_info in waiting_users[:3]:
                                            st.caption(f"- {user_info['username']}")
                                        if len(waiting_users) > 3:
                                            st.caption(f"...and {len(waiting_users) - 3} more.")
                                    else:
                                        st.caption("No buyers waiting yet.")

                                st.markdown("---")
                               
                                end_key = f"confirm_end_{a['id']}"
                                if not st.session_state.get(end_key, False):
                                    if st.button(f"🛑 End Early", key=f"end_{a['id']}", type="secondary", use_container_width=True):
                                        st.session_state[end_key] = True
                                        st.rerun()
                                else:
                                    st.error("⚠️ Confirm End Auction?")
                                    col_yes, col_no = st.columns(2)
                                    with col_yes:
                                        if st.button("✅ YES", key=f"confirm_yes_{a['id']}", type="primary", use_container_width=True):
                                          
                                            try:
                                                conn = get_db_connection()
                                                cursor = conn.cursor()
                                                
                                                raw = a.get("current_bid") or a.get("base_price")
                                                final_bid = float(raw)

                                                winner = a.get("current_bidder", "No Bids")
                                                
                                                cursor.execute("""
                                                    UPDATE auctions 
                                                    SET status='closed', end_time=UTC_TIMESTAMP(), final_bid=%s, winner=%s
                                                    WHERE id=%s
                                                """, (final_bid, winner, a["id"]))
                                                conn.commit()
                                                
                                               
                                                finalize_mongo_auction(a["product_id"], winner, float(final_bid))
                                                products_col.update_one(
                                                    {"_id": ObjectId(a["product_id"])},
                                                    {"$set": {
                                                        "status": "sold",
                                                        "sold_to": winner,
                                                        "sold_price": float(final_bid),
                                                        "sold_at": datetime.utcnow()
                                                    }}
                                                )
                                                
                                               
                                                cursor.execute("DELETE FROM auctions WHERE id=%s", (a["id"],))
                                                conn.commit()
                                                
                                            
                                                clear_waiting_room(a.get("auction_code"))
                                                
                                                st.success(f"✅ Auction ended! Winner: {winner}, Final Bid: ${final_bid}")
                                                st.session_state[end_key] = False
                                                time.sleep(1)
                                                st.rerun()
                                            except Exception as e:
                                                st.error(f"Failed to end auction: {e}")
            
                                        with col_no:
                                            if st.button("❌ NO", key=f"cancel_end_{a['id']}", use_container_width=True):
                                                st.session_state[end_key] = False
                                                st.rerun()

            with tab_closed :
                if not closed_auctions:
                    st.info("No auctions have been closed yet.")
                else:
                    for a in closed_auctions:
                        with st.container(border=True):
                            col_info, col_details = st.columns([2, 1])
                            with col_info:
                                st.markdown(f"**{a.get('product_name')}** (`{a.get('auction_code')}`)")
                                st.markdown(f"Status: <span style='color: #e74c3c;'>**CLOSED**</span>", unsafe_allow_html=True)
                                st.write(f"Winner: **{a.get('winner', 'No bids placed')}**")
                                st.success(f"Final Bid: **${a.get('final_bid', a.get('base_price'))}**")
                            with col_details:
                                st.caption(f"Started: {a.get('start_time')}")
                                if a.get('end_time'):
                                    st.caption(f"Ended: {a.get('end_time')}")

    # SELLER: Dashboard
    elif role == "Seller" and page == "Dashboard":
        st.header("📊 Seller Dashboard")
        stats = get_seller_stats(username)
        st.subheader("📈 Business Overview")
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric("Total Products", stats["total_products"])
        with col2:
            st.metric("Products Sold", stats["sold_products"],
                    delta=f"{stats['success_rate']:.1f}% Success Rate")
        with col3:
            st.metric("Total Revenue", f"${stats['total_revenue']:.2f}")
        with col4:
            st.metric("Avg Sale Price", f"${stats['avg_price']:.2f}")
        
        st.markdown("---")
        st.subheader("🔨 Auction Status")
        col1, col2, col3 = st.columns(3)
        
        col1.metric("Total Auctions", stats["total_auctions"])
        col2.metric("Active Now", stats["active_auctions"])
        col3.metric("Completed", stats["closed_auctions"])
        
        st.markdown("---")
        
        st.subheader("📦 Inventory Status")
        
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Available", stats["available_products"], help="Ready to auction")
        col2.metric("In Auction", stats["in_auction_products"], help="Currently being auctioned")
        col3.metric("Sold", stats["sold_products"], help="Successfully sold")
        col4.metric("Total", stats["total_products"])
        if stats["total_products"] > 0:
            sold_percentage = (stats["sold_products"] / stats["total_products"]) * 100
            st.write("**Sales Progress**")
            st.progress(sold_percentage / 100)
            st.caption(f"{sold_percentage:.1f}% of products sold")
        
        st.markdown("---")
        
        st.subheader("💰 Recent Sales")
        sold_products = list(products_col.find({"seller": username, "status": "sold"}).sort("sold_at", -1).limit(5))
        
        if not sold_products:
            st.info("No sales yet. Start auctioning your products!")
        else:
            for p in sold_products:
                with st.container(border=True):
                    col_img, col_info = st.columns([1, 3])
                    
                    with col_img:
                        try:
                            _, image_bytes = get_product_from_mongo(str(p["_id"]))
                            if image_bytes:
                                st.image(image_bytes, width=100)
                            else:
                                st.image("https://via.placeholder.com/100x75.png?text=Item", width=100)
                        except:
                            st.image("https://via.placeholder.com/100x75.png?text=Item", width=100)
                    
                    with col_info:
                        st.markdown(f"### {p.get('name')}")
                        st.success(f"**Sold for: ${p.get('sold_price', 0):.2f}**")
                        st.caption(f"Buyer: {p.get('sold_to', 'N/A')} | Sold: {p.get('sold_at', 'N/A')}")