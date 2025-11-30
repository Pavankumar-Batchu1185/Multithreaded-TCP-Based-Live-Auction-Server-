import socket
import asyncio
import json
import mysql.connector
from datetime import datetime
from pymongo import MongoClient
import websockets
import re
from mysql.connector import pooling
import logging
import gridfs
from bson import ObjectId
    
SERVER_IP = "127.0.0.1"
SERVER_PORT = 8000
WS_HOST = "0.0.0.0"
WS_PORT = 8765

DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "123456",
    "database": "auction_system",
    "pool_name": "auction_pool",
    "pool_size": 5
}

MONGO_URI = "mongodb+srv://pavankumarbatchu1185_db_user:Bvnspk%401185@cluster0.asbvkak.mongodb.net/"

# Setup logging 
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

#MongoDB 
mongo_client = MongoClient(MONGO_URI)
mongo_db = mongo_client["auction_data"]
active_col = mongo_db["active_auctions"]
history_col = mongo_db["auction_history"]
products_col = mongo_db["products"]
fs = gridfs.GridFS(mongo_db)
waiting_col = mongo_db["waiting_room"]
#MySQL connection pool 
connection_pool = None


def add_to_waiting_room(auction_code: str, username: str):
    """
    Adds a buyer to the waiting list for an auction (de-duplicated by username).
    """
    try:
        waiting_col.update_one(
            {"auction_code": auction_code},
            {"$addToSet": {"users": {"username": username, "joined_at": datetime.utcnow()}}},
            upsert=True
        )
    except Exception as e:
        log.error(f"Failed to add to waiting room {auction_code} : {e}")

def remove_from_waiting_room(auction_code: str, username: str):
    """
    Removes a buyer from the waiting list for an auction.
    """
    try:
        waiting_col.update_one(
            {"auction_code": auction_code},
            {"$pull": {"users": {"username": username}}}
        )
    except Exception as e:
        log.error(f"Failed to remove from waiting room {auction_code} : {e}")

def get_waiting_users(auction_code: str):
    """
    Returns a list of waiting users for auction_code: [{username, joined_at}, ...]
    """
    try:
        doc = waiting_col.find_one({"auction_code": auction_code})
        if not doc:
            return []
        return doc.get("users", [])
    except Exception as e:
        log.error(f"Failed to fetch waiting users for {auction_code} : {e}")
        return []

def clear_waiting_room(auction_code: str):
    """
    Remove the waiting room doc for the auction (optional/cleanup).
    """
    try:
        waiting_col.delete_one({"auction_code": auction_code})
    except Exception as e:
        log.error(f"Failed to clear waiting room {auction_code} : {e}")

def init_db_pool():
    global connection_pool
    if connection_pool is None:
        connection_pool = pooling.MySQLConnectionPool(**DB_CONFIG)
        log.info("MySQL connection pool initialized")

def get_db_connection():
    if connection_pool is None:
        init_db_pool()
    return connection_pool.get_connection()

def update_current_bid_by_code(new_bid: float, bidder_id: str, auction_code: str):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE auctions
            SET current_bid=%s, current_bidder=%s, last_update=UTC_TIMESTAMP()
            WHERE auction_code=%s AND status='active'
        """, (new_bid, bidder_id, auction_code))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        log.error(f"Error updating bid in MySQL for {auction_code}: {e}")

def get_closed_auctions():
    """Get closed auctions from MongoDB auction_history"""
    try:
        client = MongoClient("mongodb+srv://pavankumarbatchu1185_db_user:Bvnspk%401185@cluster0.asbvkak.mongodb.net/")
        db = client["auction_data"]
        history_col = db["auction_history"]
        
        # Get all completed auctions, sorted by closed date (newest first)
        auctions = list(history_col.find().sort("closed_at", -1))
        
        # Convert MongoDB docs to match the expected format
        result = []
        for doc in auctions:
            result.append({
                "id": str(doc.get("_id")),
                "product_id": doc.get("product_id"),
                "product_name": doc.get("product_name", "Unknown"),
                "auction_code": doc.get("auction_code", "N/A"),
                "base_price": float(doc.get("final_bid", 0)) if doc.get("winner") != "No Bids" else 0,
                "final_bid": float(doc.get("final_bid", 0)) if isinstance(doc.get("final_bid"), (int, float)) else 0,
                "winner": doc.get("winner", "No Bids"),
                "created_by": doc.get("seller", "Unknown"),
                "end_time": doc.get("closed_at"),
                "status": "closed"
            })
        
        return result
    except Exception as e:
        print(f"Error fetching closed auctions: {e}")
        return []
    
def get_product_id_by_code(auction_code: str):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT product_id FROM auctions WHERE auction_code=%s AND status='active' LIMIT 1", (auction_code,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        log.warning(f"Error looking up auction_code {auction_code}: {e}")
        return None

def log_bid_to_mongo(product_id, bidder, bid_value):
    try:
        # ensure numeric type for MongoDB storage
        try:
            bid_amount = float(bid_value)
        except Exception:
            # if it was Decimal or malformed, coerce via str->float as last resort
            bid_amount = float(str(bid_value))

        bid_entry = {
            "bidder": str(bidder),
            "amount": bid_amount,
            "timestamp": datetime.utcnow()
        }

        active_col.update_one(
            {"product_id": product_id},
            {
                "$push": {"bids": bid_entry},
                "$set": {
                    "last_bid": bid_amount,
                    "last_bidder": str(bidder),
                    "last_update": datetime.utcnow()
                }
            },
            upsert=True
        )
        log.info(f"MongoDB updated for {product_id}: {bidder} → {bid_amount}")
    except Exception as e:
        log.exception(f"Error logging to MongoDB: {e}")

def finalize_mongo_auction(product_id, winner, final_bid):
    try:
        doc = active_col.find_one({"product_id": product_id})
        product = products_col.find_one({"_id": ObjectId(product_id)})

        # Extract metadata safely
        product_name = product.get("name", "Unknown") if product else "Unknown"
        auction_code = product.get("auction_code", "N/A") if product else "N/A"
        seller = product.get("seller", "Unknown") if product else "Unknown"  # ✅ ADD THIS
        
        if doc:
            # sanitize bids and numeric fields
            bids = doc.get("bids", [])
            for b in bids:
                # convert bid amounts
                if "amount" in b:
                    try:
                        b["amount"] = float(b["amount"])
                    except Exception:
                        b["amount"] = float(str(b["amount"]))
                # ensure bidder is a string
                if "bidder" in b:
                    b["bidder"] = str(b["bidder"])
                # timestamps are fine (datetime)

            doc["winner"] = str(winner)
            doc["product_name"] = product_name
            doc["auction_code"] = auction_code
            doc["seller"] = seller  # ✅ ADD THIS
            try:
                doc["final_bid"] = float(final_bid)
            except Exception:
                doc["final_bid"] = float(str(final_bid))
            doc["closed_at"] = datetime.utcnow()

            # insert sanitized doc into history
            history_col.insert_one(doc)
            active_col.delete_one({"product_id": product_id})
            log.info(f"Moved {product_id} → auction_history (Seller: {seller})")

        # Update product status to sold
        products_col.update_one(
            {"_id": ObjectId(product_id)},
            {"$set": {
                "status": "sold",
                "sold_to": winner,
                "sold_price": float(final_bid),  
                "sold_at": datetime.utcnow()
            }}
        )
    except Exception as e:
        log.exception(f"Error finalizing auction: {e}")

# ============================================
# BUYER STATISTICS FUNCTIONS
# ============================================

def get_buyer_stats(username):
    """Get comprehensive statistics for a buyer"""
    try:
        db = mongo_db  # Already connected to Atlas
        history_col_local = db["auction_history"]
        
        # Get all completed auctions where user bid
        participated_auctions = list(history_col_local.find({
            "bids.bidder": username
        }))
        
        # Get auctions won
        won_auctions = list(history_col_local.find({
            "winner": username
        }))
        
        # Calculate statistics
        total_participated = len(participated_auctions)
        total_won = len(won_auctions)
        
        # Calculate total spent
        from bson.decimal128 import Decimal128  # Add this import at top if not there
        total_spent = 0.0
        for auction in won_auctions:
            final_bid = auction.get("final_bid", 0)
            if isinstance(final_bid, Decimal128):
                total_spent += float(final_bid.to_decimal())
            else:
                total_spent += float(final_bid)
        
        # Calculate win rate
        win_rate = (total_won / total_participated * 100) if total_participated > 0 else 0.0
        
        # Get average bid amount
        all_user_bids = []
        for auction in participated_auctions:
            user_bids = [b for b in auction.get("bids", []) if b.get("bidder") == username]
            for bid in user_bids:
                amount = bid.get("amount", 0)
                if isinstance(amount, Decimal128):
                    all_user_bids.append(float(amount.to_decimal()))
                else:
                    all_user_bids.append(float(amount))
        
        avg_bid = sum(all_user_bids) / len(all_user_bids) if all_user_bids else 0.0
        
        return {
            "total_participated": total_participated,
            "total_won": total_won,
            "total_spent": total_spent,
            "win_rate": win_rate,
            "avg_bid": avg_bid,
            "won_auctions": won_auctions,
            "participated_auctions": participated_auctions
        }
    except Exception as e:
        log.error(f"Error getting buyer stats: {e}")  # Use log instead of print
        return {
            "total_participated": 0,
            "total_won": 0,
            "total_spent": 0.0,
            "win_rate": 0.0,
            "avg_bid": 0.0,
            "won_auctions": [],
            "participated_auctions": []
        }


# ============================================
# SELLER STATISTICS FUNCTIONS
# ============================================

def get_seller_stats(username):
    """Get comprehensive statistics for a seller"""
    try:
        db = mongo_db  # Already connected to Atlas
        history_col_local = db["auction_history"]
        
        # Get products by seller (use global products_col)
        total_products = products_col.count_documents({"seller": username})
        available_products = products_col.count_documents({"seller": username, "status": "available"})
        in_auction_products = products_col.count_documents({"seller": username, "status": "in_auction"})
        sold_products = products_col.count_documents({"seller": username, "status": "sold"})
        
        # Get MySQL auctions
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT COUNT(*) as total FROM auctions WHERE created_by=%s", (username,))
        total_auctions = cursor.fetchone()["total"]
        
        cursor.execute("SELECT COUNT(*) as active FROM auctions WHERE created_by=%s AND status='active'", (username,))
        active_auctions = cursor.fetchone()["active"]
        
        cursor.execute("SELECT COUNT(*) as closed FROM auctions WHERE created_by=%s AND status='closed'", (username,))
        closed_auctions = cursor.fetchone()["closed"]
        cursor.close()
        conn.close()
        
        # Get completed auctions from MongoDB to calculate revenue
        completed = list(history_col_local.find({
            "seller": username
        }))
        
        # Calculate total revenue
        from bson.decimal128 import Decimal128  # Add this import at top if not there
        total_revenue = 0.0
        for auction in completed:
            if auction.get("winner") != "No Bids":
                final_bid = auction.get("final_bid", 0)
                if isinstance(final_bid, Decimal128):
                    total_revenue += float(final_bid.to_decimal())
                else:
                    total_revenue += float(final_bid)
        
        # Calculate average selling price
        avg_price = total_revenue / sold_products if sold_products > 0 else 0.0
        
        # Success rate (sold / total auctions)
        success_rate = (sold_products / total_auctions * 100) if total_auctions > 0 else 0.0
        
        return {
            "total_products": total_products,
            "available_products": available_products,
            "in_auction_products": in_auction_products,
            "sold_products": sold_products,
            "total_auctions": total_auctions,
            "active_auctions": active_auctions,
            "closed_auctions": closed_auctions,
            "total_revenue": total_revenue,
            "avg_price": avg_price,
            "success_rate": success_rate
        }
    except Exception as e:
        log.error(f"Error getting seller stats: {e}")  # Use log instead of print
        return {
            "total_products": 0,
            "available_products": 0,
            "in_auction_products": 0,
            "sold_products": 0,
            "total_auctions": 0,
            "active_auctions": 0,
            "closed_auctions": 0,
            "total_revenue": 0.0,
            "avg_price": 0.0,
            "success_rate": 0.0
        }
    
def save_product_to_mongo(seller, name, description, base_price, image_bytes):
    image_file_id = fs.put(image_bytes)

    product_doc = {
        "seller": seller,
        "name": name,
        "description": description,
        "base_price": base_price,
        "image_file_id": image_file_id,
        "ai_generated_score": None,
        "ai_flag": False,
        "created_at": datetime.utcnow(),
        "status": "available"
    }

    result = products_col.insert_one(product_doc)
    return str(result.inserted_id)

def get_product_from_mongo(product_id):
    product = products_col.find_one({"_id": ObjectId(product_id)})
    if not product:
        return None

    image_bytes = fs.get(product["image_file_id"]).read()

    return product, image_bytes

def delete_product_from_mongo(product_id):
    try:
        doc = products_col.find_one({"_id": ObjectId(product_id)})
        if not doc:
            return
        
        try:
            fs.delete(doc["image_file_id"])
        except:
            pass
        
        products_col.delete_one({"_id": ObjectId(product_id)})
        log.info(f"Deleted product {product_id} from MongoDB")
    except Exception as e:
        log.error(f"Error deleting product from MongoDB: {e}")

def parse_bid_message(msg: str):
    try:
        msg = msg.strip()
        # NEW HIGH BID! <amount> by <username> in <AUC-XXXX>
        m = re.search(r'NEW\s+HIGH\s+BID!\s*([0-9]+(?:\.[0-9]+)?)\s+by\s+(.+?)\s+in\s+(AUC-[A-Z0-9]+)', msg, re.IGNORECASE)
        if m:
            bid = float(m.group(1))
            bidder = m.group(2).strip()
            auction_code = m.group(3).strip()
            return bid, bidder, auction_code

        # [JOIN] <username> joined <AUC-XXXX>
        m2 = re.search(r'\[JOIN\]\s+(.+?)\s+joined\s+(AUC-[A-Z0-9]+)', msg, re.IGNORECASE)
        if m2:
            username = m2.group(1).strip()
            auction_code = m2.group(2).strip()
            return None, {"type": "join", "username": username, "auction_code": auction_code}, None
    except Exception as e:
        log.warning(f"Parse error: {e} — msg: {msg}")
    return None, None, None

connected_websockets = set()

async def ws_handler(websocket):
    connected_websockets.add(websocket)
    remote = websocket.remote_address
    log.info(f"WebSocket client connected from {remote}")
    try:
        async for message in websocket:
            log.debug(f"WS message from {remote}: {message}")
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        log.warning(f"WebSocket error: {e}")
    finally:
        connected_websockets.discard(websocket)
        log.info(f"WebSocket client disconnected from {remote}")

async def broadcast_ws(msg_dict):
    if not connected_websockets:
        return
    payload = json.dumps(msg_dict, default=str)
    disconnected = set()
    for ws in connected_websockets:
        try:
            await ws.send(payload)
        except websockets.exceptions.ConnectionClosed:
            disconnected.add(ws)
        except Exception as e:
            log.warning(f"Error sending to WebSocket: {e}")
            disconnected.add(ws)
    connected_websockets.difference_update(disconnected)

async def tcp_monitor_loop():
    retry_delay = 5
    while True:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        try:
            log.info(f"🔌 Connecting to Auction Server at {SERVER_IP}:{SERVER_PORT}...")
            sock.connect((SERVER_IP, SERVER_PORT))
            sock.settimeout(None)
            sock.sendall(b"MONITOR_CLIENT\n")
            log.info("Connected to Auction Server as Monitor Client")
            buffer = ""
            loop = asyncio.get_running_loop()
            while True:
                data = await loop.run_in_executor(None, sock.recv, 1024)
                if not data:
                    log.warning("Connection closed by server")
                    break
                buffer += data.decode(errors="ignore")
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    msg = line.strip()
                    if not msg:
                        continue
                    log.info(f"[TCP BROADCAST] {msg}")
                    bid, bidder, auction_code = parse_bid_message(msg)
                    if bid is not None and bidder and auction_code:
                        product_id = get_product_id_by_code(auction_code)
                        if product_id:
                            update_current_bid_by_code(bid, bidder, auction_code)
                            log_bid_to_mongo(product_id, bidder, bid)
                            update = {
                                "type": "bid_update",
                                "auction_code": auction_code,
                                "product_id": product_id,
                                "bid": bid,
                                "bidder": bidder,
                                "timestamp": datetime.utcnow().isoformat()
                            }
                            await broadcast_ws(update)
                    elif isinstance(bidder, dict) and bidder.get("type") == "join":
                        join_update = {
                            "type": "user_joined",
                            "username": bidder["username"],
                            "auction_code": bidder["auction_code"],
                            "timestamp": datetime.utcnow().isoformat()
                        }
                        await broadcast_ws(join_update)
        except ConnectionRefusedError:
            log.error(f"Could not connect to Auction Server. Retrying in {retry_delay}s...")
        except socket.timeout:
            log.warning(f"Connection timeout. Retrying in {retry_delay}s...")
        except Exception as e:
            log.exception(f"Error in TCP monitor: {e}")
        finally:
            try:
                sock.close()
            except Exception:
                pass
            log.info(f"🔌 Disconnected from Auction Server. Retrying in {retry_delay}s...")
        await asyncio.sleep(retry_delay)

async def main():
    init_db_pool()
    log.info(f"Starting WebSocket server on ws://{WS_HOST}:{WS_PORT}")
    ws_server = await websockets.serve(ws_handler, WS_HOST, WS_PORT)
    log.info("WebSocket server ready")
    tcp_task = asyncio.create_task(tcp_monitor_loop())
    try:
        await tcp_task
    except asyncio.CancelledError:
        log.info("TCP monitor cancelled")
    finally:
        ws_server.close()
        await ws_server.wait_closed()
        log.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("\n\nShutting down gracefully...")
    except Exception as e:
        log.exception(f" Fatal error: {e}")