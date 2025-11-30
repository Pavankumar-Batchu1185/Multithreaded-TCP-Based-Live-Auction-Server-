# email_sender.py — Real user emails, production-ready
import os
import smtplib
import time
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import mysql.connector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


SMTP_USER = os.getenv("SMTP_USER")          #  main email for sending
SMTP_PASS = os.getenv("SMTP_PASS")          # App password
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))


DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "123456"),
    "database":  "auction_system"
}

EMAIL_SEND_DELAY = float(os.getenv("EMAIL_SEND_DELAY", "0.5"))

def send_email(to_email: str, subject: str, body: str) -> bool:
    if not SMTP_USER or not SMTP_PASS:
        logging.error("❌ SMTP credentials not configured. Set SMTP_USER and SMTP_PASS env vars.")
        return False

    if not to_email or "@" not in to_email:
        logging.warning(f"⚠️ Invalid email address: {to_email}")
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "html"))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20)
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [to_email], msg.as_string())
        server.quit()

        logging.info(f"✅ Email sent to {to_email}")
        return True

    except smtplib.SMTPAuthenticationError:
        logging.error("❌ SMTP Authentication failed. Check your username/password.")
        return False
    except smtplib.SMTPException as e:
        logging.error(f"❌ SMTP error sending to {to_email}: {e}")
        return False
    except Exception as e:
        logging.exception(f"❌ Failed to send email to {to_email}: {e}")
        return False

def get_buyer_emails():
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("""
            SELECT email FROM users 
            WHERE role='Buyer' 
            AND email IS NOT NULL 
            AND email != ''
        """)
        
        buyers = cursor.fetchall()
        cursor.close()
        conn.close()
        
        emails = [b["email"] for b in buyers if b.get("email")]
        logging.info(f"📧 Found {len(emails)} buyer email(s)")
        return emails
        
    except Exception as e:
        logging.exception(f"❌ Failed to fetch buyer emails: {e}")
        return []

def notify_buyers(product_name, auction_code, start_time, duration_minutes, meet_link, base_price):
    subject = f"🔔 New Auction Alert: {product_name} ({auction_code})"
    
    body = f"""
<html>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 2px solid #4CAF50; border-radius: 10px;">
        <h2 style="color: #4CAF50; text-align: center;">🎉 New Live Auction Started!</h2>
        <hr style="border: 1px solid #4CAF50;">
        
        <p style="font-size: 16px;"><strong>Dear Bidder,</strong></p>
        <p>A new exciting auction is now live! Don't miss your chance to bid.</p>
        
        <div style="background-color: #f9f9f9; padding: 15px; border-radius: 5px; margin: 20px 0;">
            <p><strong>📦 Product:</strong> {product_name}</p>
            <p><strong>🔑 Auction Code:</strong> <span style="color: #4CAF50; font-size: 18px; font-weight: bold;">{auction_code}</span></p>
            <p><strong>💰 Starting Price:</strong> ${base_price}</p>
            <p><strong>⏰ Duration:</strong> {duration_minutes} minutes</p>
            <p><strong>🕐 Start Time (UTC):</strong> {start_time}</p>
        </div>
        
        <div style="text-align: center; margin: 30px 0;">
            <a href="{meet_link}" style="background-color: #4CAF50; color: white; padding: 12px 30px; text-decoration: none; border-radius: 5px; font-size: 16px; display: inline-block;">
                🎥 Join Google Meet
            </a>
        </div>
        
        <p style="font-size: 14px; color: #666; text-align: center;">
            Use the auction code above to join the bidding room!
        </p>
        
        <hr style="border: 1px solid #eee; margin-top: 20px;">
        <p style="font-size: 12px; color: #999; text-align: center;">
            You're receiving this because you're registered as a buyer on BidVerse.
        </p>
    </div>
</body>
</html>
"""
    buyer_emails = get_buyer_emails()
    
    if not buyer_emails:
        logging.warning("⚠️ No buyer emails found to notify.")
        return 0, 0

    success = 0
    total = len(buyer_emails)
    
    logging.info(f"📨 Sending auction notification to {total} buyer(s)...")

    for email in buyer_emails:
        if send_email(email, subject, body):
            success += 1
        time.sleep(EMAIL_SEND_DELAY)  # Prevent rate limiting

    logging.info(f"✅ Notification complete: {success}/{total} emails sent for auction {auction_code}")
    return success, total

def notify_seller(seller_email: str, product_name: str, winner: str, final_bid: float):
    if not seller_email:
        logging.warning("⚠️ No seller email provided")
        return False
    
    subject = f"✅ Auction Complete: {product_name}"
    
    body = f"""
<html>
<body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 2px solid #2196F3; border-radius: 10px;">
        <h2 style="color: #2196F3; text-align: center;">🎉 Your Auction Has Ended</h2>
        <hr style="border: 1px solid #2196F3;">
        
        <div style="background-color: #f0f8ff; padding: 15px; border-radius: 5px; margin: 20px 0;">
            <p><strong>📦 Product:</strong> {product_name}</p>
            <p><strong>🏆 Winner:</strong> {winner}</p>
            <p><strong>💰 Final Price:</strong> <span style="color: #4CAF50; font-size: 20px; font-weight: bold;">${final_bid:.2f}</span></p>
        </div>
        
        <p style="text-align: center; margin: 20px 0;">
            Congratulations on completing your auction!
        </p>
        
        <hr style="border: 1px solid #eee; margin-top: 20px;">
        <p style="font-size: 12px; color: #999; text-align: center;">
            BidVerse - Live Auction Platform
        </p>
    </div>
</body>
</html>
"""
    
    result = send_email(seller_email, subject, body)
    
    if result:
        logging.info(f"✅ Seller notification sent to {seller_email}")
    else:
        logging.error(f"❌ Failed to send seller notification to {seller_email}")
    
    return result


def notify_winner(winner_email: str, product_name: str, final_bid: float, auction_code: str):
 
    if not winner_email:
        logging.warning("⚠️ No winner email provided")
        return False
    
    subject = f"🎉 Congratulations! You won: {product_name}"
    
    body = f"""
<html>
<body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 2px solid #FFD700; border-radius: 10px;">
        <h2 style="color: #FFD700; text-align: center;">🏆 Congratulations! You Won!</h2>
        <hr style="border: 1px solid #FFD700;">
        
        <p style="font-size: 16px;"><strong>Dear Winner,</strong></p>
        <p>You have successfully won the auction!</p>
        
        <div style="background-color: #fffbea; padding: 15px; border-radius: 5px; margin: 20px 0;">
            <p><strong>📦 Product:</strong> {product_name}</p>
            <p><strong>🔑 Auction Code:</strong> {auction_code}</p>
            <p><strong>💰 Your Winning Bid:</strong> <span style="color: #4CAF50; font-size: 20px; font-weight: bold;">${final_bid:.2f}</span></p>
        </div>
        
        <p style="text-align: center; margin: 20px 0;">
            The seller will contact you shortly with next steps.
        </p>
        
        <hr style="border: 1px solid #eee; margin-top: 20px;">
        <p style="font-size: 12px; color: #999; text-align: center;">
            Thank you for using BidVerse!
        </p>
    </div>
</body>
</html>
"""
    
    result = send_email(winner_email, subject, body)
    
    if result:
        logging.info(f"✅ Winner notification sent to {winner_email}")
    else:
        logging.error(f"❌ Failed to send winner notification to {winner_email}")
    
    return result

def test_email_config(test_recipient: str = None):
  
    recipient = test_recipient or SMTP_USER
    
    if not recipient:
        logging.error("❌ No test recipient provided and SMTP_USER not set")
        return False
    
    subject = "✅ BidVerse Email Test"
    body = """
<html>
<body style="font-family: Arial, sans-serif;">
    <h2 style="color: #4CAF50;">Email Configuration Test</h2>
    <p>If you're reading this, your email configuration is working correctly! ✅</p>
    <p><strong>SMTP Server:</strong> {}</p>
    <p><strong>SMTP Port:</strong> {}</p>
</body>
</html>
""".format(SMTP_SERVER, SMTP_PORT)
    
    logging.info(f"📧 Sending test email to {recipient}...")
    result = send_email(recipient, subject, body)
    
    if result:
        logging.info("✅ Test email sent successfully!")
    else:
        logging.error("❌ Test email failed!")
    
    return result


if __name__ == "__main__":
    print("🧪 Testing email configuration...\n")
    
    
    if not SMTP_USER or not SMTP_PASS:
        print("❌ ERROR: SMTP credentials not set!")
        print("\nPlease set these environment variables:")
        print("  export SMTP_USER='your-email@gmail.com'")
        print("  export SMTP_PASS='your-app-password'")
        print("\nFor Gmail, generate an App Password at:")
        print("  https://myaccount.google.com/apppasswords")
    else:
        print(f"📧 SMTP User: {SMTP_USER}")
        print(f"🔧 SMTP Server: {SMTP_SERVER}:{SMTP_PORT}\n")

        test_email = input("Enter email to test (or press Enter to use SMTP_USER): ").strip()
        test_email_config(test_email if test_email else None)