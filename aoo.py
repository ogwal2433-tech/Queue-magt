from flask import Flask, request, jsonify
from flask_cors import CORS
import mysql.connector
from mysql.connector import pooling
from datetime import datetime
import logging
import os

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__)
CORS(app)

# ============================================
# DATABASE CONFIGURATION
# ============================================

DB_CONFIG = {
    'host': 'localhost',
    'port': 3306,
    'user': 'root',
    'password': '',  # Change to your password
    'database': 'queue_management_system',
    'pool_name': 'queue_pool',
    'pool_size': 10
}

# Create connection pool
try:
    connection_pool = pooling.MySQLConnectionPool(**DB_CONFIG)
    print("✅ Database connected successfully")
except Exception as e:
    print(f"❌ Database connection error: {e}")
    connection_pool = None

def get_db_connection():
    """Get database connection from pool"""
    if connection_pool:
        return connection_pool.get_connection()
    else:
        return mysql.connector.connect(**DB_CONFIG)

def execute_query(query, params=None, fetch_one=False, fetch_all=False, commit=False):
    """Execute SQL query and return results"""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        cursor.execute(query, params or ())
        if fetch_one:
            result = cursor.fetchone()
        elif fetch_all:
            result = cursor.fetchall()
        else:
            result = None
        
        if commit:
            conn.commit()
            result = cursor.lastrowid
        
        return result
    except Exception as e:
        if commit:
            conn.rollback()
        raise e
    finally:
        cursor.close()
        conn.close()

# ============================================
# HEALTH CHECK
# ============================================

@app.route('/api/health', methods=['GET'])
def health_check():
    """Check if API and database are running"""
    try:
        result = execute_query("SELECT 1 as test", fetch_one=True)
        db_status = "connected" if result else "error"
    except Exception as e:
        logger.error(f"Database error: {e}")
        db_status = "disconnected"
    
    return jsonify({
        'status': 'ok',
        'database': db_status,
        'timestamp': datetime.now().isoformat()
    })

# ============================================
# TOKEN GENERATION
# ============================================

# ============================================
# TOKEN GENERATION - WITH DUPLICATE CHECK
# ============================================

@app.route('/api/tokens', methods=['POST'])
def generate_token():
    """Generate a new queue token for customer"""
    data = request.get_json()
    
    service_category = data.get('service_category')
    customer_name = data.get('customer_name')
    customer_phone = data.get('customer_phone')
    source = data.get('source', 'form')
    
    if not service_category:
        return jsonify({'success': False, 'message': 'Service category is required'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        # Get service details
        cursor.execute("""
            SELECT category_name, estimated_service_time, prefix, next_number
            FROM service_categories 
            WHERE category_code = %s AND is_active = 1
        """, (service_category,))
        
        service = cursor.fetchone()
        
        if not service:
            return jsonify({'success': False, 'message': 'Invalid service category'}), 400
        
        # Generate unique token number (skip existing ones)
        token_number = None
        max_attempts = 100  # Prevent infinite loop
        
        for attempt in range(max_attempts):
            next_num = service['next_number']
            candidate_token = f"{service['prefix']}{str(next_num).zfill(3)}"
            
            # Check if token already exists
            cursor.execute("SELECT COUNT(*) as count FROM queue_tokens WHERE token_number = %s", (candidate_token,))
            exists = cursor.fetchone()
            
            if exists['count'] == 0:
                token_number = candidate_token
                # Increment next_number for next customer
                cursor.execute("""
                    UPDATE service_categories 
                    SET next_number = next_number + 1
                    WHERE category_code = %s
                """, (service_category,))
                break
            else:
                # Token exists, increment and try again
                cursor.execute("""
                    UPDATE service_categories 
                    SET next_number = next_number + 1
                    WHERE category_code = %s
                """, (service_category,))
                # Refresh service next_number
                cursor.execute("""
                    SELECT next_number FROM service_categories 
                    WHERE category_code = %s
                """, (service_category,))
                service['next_number'] = cursor.fetchone()['next_number']
        
        if not token_number:
            return jsonify({'success': False, 'message': 'Failed to generate unique token'}), 500
        
        # Count customers ahead
        cursor.execute("""
            SELECT COUNT(*) as ahead_count
            FROM queue_tokens
            WHERE service_category = %s
            AND status IN ('waiting', 'called')
        """, (service_category,))
        
        ahead = cursor.fetchone()
        ahead_count = ahead['ahead_count'] if ahead else 0
        estimated_wait = ahead_count * service['estimated_service_time']
        queue_position = ahead_count + 1
        
        # Insert new token
        cursor.execute("""
            INSERT INTO queue_tokens (
                token_number, service_category, customer_name, customer_phone, 
                status, queue_position, estimated_wait_minutes, source, requested_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """, (
            token_number, service_category, customer_name,
            customer_phone, 'waiting', queue_position, estimated_wait, source
        ))
        
        conn.commit()
        
        logger.info(f"✅ Token generated: {token_number} for service {service_category}")
        
        return jsonify({
            'success': True,
            'token_number': token_number,
            'service_name': service['category_name'],
            'queue_position': queue_position,
            'ahead_count': ahead_count,
            'estimated_wait': estimated_wait,
            'message': 'Token generated successfully'
        })
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Error generating token: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# ============================================
# GET QUEUE POSITION
# ============================================

@app.route('/api/queue/position/<token_number>', methods=['GET'])
def get_queue_position(token_number):
    """Get current queue position and status for a token"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("""
            SELECT q.*, t.teller_number as assigned_teller
            FROM queue_tokens q
            LEFT JOIN tellers t ON q.assigned_teller_id = t.id
            WHERE q.token_number = %s
        """, (token_number,))
        
        token = cursor.fetchone()
        
        if not token:
            return jsonify({'success': False, 'message': 'Token not found'}), 404
        
        cursor.execute("""
            SELECT COUNT(*) as ahead_count
            FROM queue_tokens
            WHERE service_category = %s
            AND status IN ('waiting', 'called')
            AND requested_at < %s
        """, (token['service_category'], token['requested_at']))
        
        ahead = cursor.fetchone()
        ahead_count = ahead['ahead_count'] if ahead else 0
        
        service_times = {'W': 3, 'L': 15, 'D': 5, 'C': 20, 'T': 8, 'E': 4}
        avg_service_time = service_times.get(token['service_category'], 5)
        estimated_wait = ahead_count * avg_service_time
        
        cursor.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'token_number': token['token_number'],
            'status': token['status'],
            'queue_position': ahead_count + 1,
            'ahead_count': ahead_count,
            'estimated_wait': estimated_wait,
            'teller_number': token['assigned_teller']
        })
        
    except Exception as e:
        logger.error(f"Error getting queue position: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

# ============================================
# GET CURRENT QUEUE STATUS
# ============================================

@app.route('/api/queue/current', methods=['GET'])
def get_current_queue():
    """Get current queue status for all services"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("""
            SELECT 
                sc.category_code,
                sc.category_name,
                sc.color_code,
                sc.icon,
                COUNT(CASE WHEN q.status = 'waiting' THEN 1 END) as waiting_count,
                COUNT(CASE WHEN q.status = 'called' THEN 1 END) as called_count,
                COUNT(CASE WHEN q.status = 'serving' THEN 1 END) as serving_count,
                (SELECT token_number FROM queue_tokens q2 
                 WHERE q2.service_category = sc.category_code AND q2.status = 'serving'
                 LIMIT 1) as current_serving
            FROM service_categories sc
            LEFT JOIN queue_tokens q ON q.service_category = sc.category_code
            WHERE sc.is_active = 1
            GROUP BY sc.category_code, sc.category_name, sc.color_code, sc.icon
            ORDER BY sc.display_order
        """)
        
        queue_data = cursor.fetchall()
        cursor.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'queue': queue_data,
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error getting queue status: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

# ============================================
# GET WAITING CUSTOMERS FOR A SERVICE
# ============================================

@app.route('/api/queue/waiting/<service_category>', methods=['GET'])
def get_waiting_customers(service_category):
    """Get all waiting customers for a specific service"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("""
            SELECT token_number, customer_name, customer_phone, requested_at
            FROM queue_tokens
            WHERE service_category = %s
            AND status = 'waiting'
            ORDER BY requested_at ASC
        """, (service_category,))
        
        waiting = cursor.fetchall()
        cursor.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'waiting': waiting,
            'count': len(waiting)
        })
        
    except Exception as e:
        logger.error(f"Error getting waiting customers: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

# ============================================
# GET ALL TELLERS
# ============================================

@app.route('/api/tellers', methods=['GET'])
def get_tellers():
    """Get all tellers with their current status"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("""
            SELECT 
                t.id,
                t.teller_number,
                t.teller_name,
                t.email,
                t.phone,
                t.pin_code,
                t.status,
                t.current_token,
                t.current_token_id,
                t.serving_category,
                sc.category_name as service_name,
                t.last_activity,
                TIMESTAMPDIFF(MINUTE, t.last_activity, NOW()) as idle_minutes
            FROM tellers t
            LEFT JOIN service_categories sc ON t.serving_category = sc.category_code
            ORDER BY t.teller_number
        """)
        
        tellers = cursor.fetchall()
        cursor.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'tellers': tellers
        })
        
    except Exception as e:
        logger.error(f"Error getting tellers: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

# ============================================
# TELLER: LOGIN
# ============================================

@app.route('/api/tellers/login', methods=['POST'])
def teller_login():
    """Authenticate teller with number and PIN"""
    data = request.get_json()
    
    teller_number = data.get('teller_number')
    pin_code = data.get('pin_code')
    
    if not teller_number or not pin_code:
        return jsonify({'success': False, 'message': 'Teller number and PIN required'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        cursor.execute("""
            SELECT id, teller_number, teller_name, serving_category, email, phone, status
            FROM tellers
            WHERE teller_number = %s AND pin_code = %s
        """, (teller_number, pin_code))
        
        teller = cursor.fetchone()
        
        if teller:
            return jsonify({
                'success': True,
                'teller': teller,
                'message': 'Login successful'
            })
        else:
            return jsonify({
                'success': False,
                'message': 'Invalid teller number or PIN'
            }), 401
            
    except Exception as e:
        logger.error(f"Login error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# ============================================
# TELLER: CALL NEXT CUSTOMER
# ============================================

@app.route('/api/tellers/call-next', methods=['POST'])
def call_next_customer():
    """Teller calls the next waiting customer"""
    data = request.get_json()
    
    teller_id = data.get('teller_id')
    teller_number = data.get('teller_number')
    service_category = data.get('service_category')
    
    if not all([teller_id, teller_number, service_category]):
        return jsonify({'success': False, 'message': 'Missing required fields'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        cursor.execute("""
            SELECT id, token_number, customer_name, customer_phone
            FROM queue_tokens
            WHERE service_category = %s
            AND status = 'waiting'
            ORDER BY requested_at ASC
            LIMIT 1
        """, (service_category,))
        
        next_token = cursor.fetchone()
        
        if not next_token:
            return jsonify({'success': False, 'message': 'No customers waiting'}), 200
        
        cursor.execute("""
            UPDATE queue_tokens 
            SET status = 'called', 
                called_at = NOW(),
                assigned_teller_id = %s,
                assigned_teller_number = %s
            WHERE id = %s
        """, (teller_id, teller_number, next_token['id']))
        
        cursor.execute("""
            UPDATE tellers 
            SET status = 'called',
                current_token = %s,
                current_token_id = %s,
                last_activity = NOW()
            WHERE id = %s
        """, (next_token['token_number'], next_token['id'], teller_id))
        
        conn.commit()
        
        logger.info(f"Teller {teller_number} called customer {next_token['token_number']}")
        
        return jsonify({
            'success': True,
            'token_number': next_token['token_number'],
            'customer_name': next_token['customer_name'],
            'teller_number': teller_number,
            'message': f"Customer {next_token['token_number']} has been called"
        })
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Error calling next customer: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# ============================================
# TELLER: START SERVING CUSTOMER
# ============================================

@app.route('/api/tellers/serve', methods=['POST'])
def serve_customer():
    """Teller starts serving the customer"""
    data = request.get_json()
    
    teller_id = data.get('teller_id')
    token_number = data.get('token_number')
    
    if not all([teller_id, token_number]):
        return jsonify({'success': False, 'message': 'Missing required fields'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        cursor.execute("""
            UPDATE queue_tokens 
            SET status = 'serving', 
                serving_started_at = NOW()
            WHERE token_number = %s
        """, (token_number,))
        
        cursor.execute("""
            UPDATE tellers 
            SET status = 'busy',
                last_activity = NOW()
            WHERE id = %s
        """, (teller_id,))
        
        conn.commit()
        
        logger.info(f"Teller {teller_id} started serving {token_number}")
        
        return jsonify({
            'success': True,
            'token_number': token_number,
            'message': f"Customer {token_number} is now being served"
        })
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Error serving customer: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# ============================================
# TELLER: COMPLETE SERVICE
# ============================================

@app.route('/api/tellers/complete', methods=['POST'])
def complete_service():
    """Teller completes service for a customer"""
    data = request.get_json()
    
    teller_id = data.get('teller_id')
    token_number = data.get('token_number')
    
    if not all([teller_id, token_number]):
        return jsonify({'success': False, 'message': 'Missing required fields'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        cursor.execute("""
            UPDATE queue_tokens 
            SET status = 'completed', 
                completed_at = NOW(),
                service_duration_minutes = TIMESTAMPDIFF(MINUTE, serving_started_at, NOW()),
                total_time_minutes = TIMESTAMPDIFF(MINUTE, requested_at, NOW())
            WHERE token_number = %s
        """, (token_number,))
        
        cursor.execute("""
            UPDATE tellers 
            SET status = 'available',
                current_token = NULL,
                current_token_id = NULL,
                last_activity = NOW()
            WHERE id = %s
        """, (teller_id,))
        
        conn.commit()
        
        logger.info(f"Teller {teller_id} completed service for {token_number}")
        
        return jsonify({
            'success': True,
            'token_number': token_number,
            'message': f"Service completed for {token_number}"
        })
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Error completing service: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# ============================================
# TELLER: SKIP CUSTOMER
# ============================================

@app.route('/api/tellers/skip', methods=['POST'])
def skip_customer():
    """Teller skips current customer"""
    data = request.get_json()
    
    teller_id = data.get('teller_id')
    token_number = data.get('token_number')
    
    if not all([teller_id, token_number]):
        return jsonify({'success': False, 'message': 'Missing required fields'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        cursor.execute("""
            SELECT status FROM queue_tokens WHERE token_number = %s
        """, (token_number,))
        token = cursor.fetchone()
        
        if token:
            cursor.execute("""
                UPDATE queue_tokens 
                SET status = 'skipped', 
                    skipped_at = NOW()
                WHERE token_number = %s
            """, (token_number,))
        
        cursor.execute("""
            UPDATE tellers 
            SET status = 'available',
                current_token = NULL,
                current_token_id = NULL,
                last_activity = NOW()
            WHERE id = %s
        """, (teller_id,))
        
        conn.commit()
        
        logger.info(f"Teller {teller_id} skipped customer {token_number}")
        
        return jsonify({
            'success': True,
            'token_number': token_number,
            'message': f"Customer {token_number} has been skipped"
        })
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Error skipping customer: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# ============================================
# TELLER: RECALL CUSTOMER
# ============================================

@app.route('/api/tellers/recall', methods=['POST'])
def recall_customer():
    """Recall customer (repeat announcement)"""
    data = request.get_json()
    
    teller_id = data.get('teller_id')
    token_number = data.get('token_number')
    
    if not all([teller_id, token_number]):
        return jsonify({'success': False, 'message': 'Missing required fields'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        cursor.execute("""
            SELECT status FROM queue_tokens WHERE token_number = %s
        """, (token_number,))
        token = cursor.fetchone()
        
        if not token:
            return jsonify({'success': False, 'message': 'Token not found'}), 404
        
        cursor.execute("""
            INSERT INTO queue_logs (token_number, teller_id, action, action_details)
            VALUES (%s, %s, 'recall', 'Recall announcement sent')
        """, (token_number, teller_id))
        
        conn.commit()
        
        return jsonify({
            'success': True,
            'token_number': token_number,
            'status': token['status'],
            'message': f"Recall announcement for {token_number}"
        })
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Error recalling customer: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# ============================================
# TELLER MANAGEMENT ENDPOINTS
# ============================================
# ============================================
# GET WAITING TOKENS LIST FOR A SERVICE
# ============================================

@app.route('/api/queue/waiting-list/<service_category>', methods=['GET'])
def get_waiting_list(service_category):
    """Get all waiting tokens with details for a specific service"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("""
            SELECT 
                id,
                token_number,
                customer_name,
                customer_phone,
                requested_at,
                TIMESTAMPDIFF(MINUTE, requested_at, NOW()) as waiting_minutes
            FROM queue_tokens
            WHERE service_category = %s
            AND status = 'waiting'
            ORDER BY requested_at ASC
        """, (service_category,))
        
        waiting_tokens = cursor.fetchall()
        cursor.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'waiting_tokens': waiting_tokens,
            'count': len(waiting_tokens)
        })
        
    except Exception as e:
        logger.error(f"Error getting waiting list: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    # ============================================
# TELLER: CALL SPECIFIC CUSTOMER
# ============================================
# ============================================
# GET RECENT RECALLS
# ============================================

@app.route('/api/queue/recent-recalls', methods=['GET'])
def get_recent_recalls():
    """Get recent recall events in the last 30 seconds"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("""
            SELECT token_number, teller_id, action_details, created_at
            FROM queue_logs
            WHERE action = 'recall'
            AND created_at > DATE_SUB(NOW(), INTERVAL 30 SECOND)
            ORDER BY created_at DESC
        """)
        
        recalls = cursor.fetchall()
        cursor.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'recalls': recalls
        })
        
    except Exception as e:
        logger.error(f"Error getting recalls: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/tellers/call-specific', methods=['POST'])
def call_specific_customer():
    """Teller calls a specific customer by token number"""
    data = request.get_json()
    
    teller_id = data.get('teller_id')
    teller_number = data.get('teller_number')
    token_number = data.get('token_number')
    
    if not all([teller_id, teller_number, token_number]):
        return jsonify({'success': False, 'message': 'Missing required fields'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        # Get token details
        cursor.execute("""
            SELECT id, token_number, customer_name, service_category
            FROM queue_tokens
            WHERE token_number = %s AND status = 'waiting'
        """, (token_number,))
        
        token = cursor.fetchone()
        
        if not token:
            return jsonify({'success': False, 'message': 'Token not found or already called'}), 404
        
        # Update token status to 'called'
        cursor.execute("""
            UPDATE queue_tokens 
            SET status = 'called', 
                called_at = NOW(),
                assigned_teller_id = %s,
                assigned_teller_number = %s
            WHERE id = %s
        """, (teller_id, teller_number, token['id']))
        
        # Update teller status
        cursor.execute("""
            UPDATE tellers 
            SET status = 'called',
                current_token = %s,
                current_token_id = %s,
                last_activity = NOW()
            WHERE id = %s
        """, (token_number, token['id'], teller_id))
        
        conn.commit()
        
        logger.info(f"Teller {teller_number} called customer {token_number}")
        
        return jsonify({
            'success': True,
            'token_number': token_number,
            'customer_name': token['customer_name'],
            'teller_number': teller_number,
            'message': f"Customer {token_number} has been called"
        })
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Error calling specific customer: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/tellers', methods=['POST'])
def add_teller():
    """Add a new teller"""
    data = request.get_json()
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        cursor.execute("""
            INSERT INTO tellers (teller_number, teller_name, email, phone, serving_category, pin_code, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (data['teller_number'], data['teller_name'], data.get('email'), 
              data.get('phone'), data['serving_category'], data.get('pin_code', '1234'), 'available'))
        conn.commit()
        
        return jsonify({'success': True, 'message': 'Teller added successfully'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/tellers/<int:teller_id>', methods=['PUT'])
def update_teller(teller_id):
    """Update teller information"""
    data = request.get_json()
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        cursor.execute("""
            UPDATE tellers 
            SET teller_number = %s, teller_name = %s, email = %s, phone = %s, serving_category = %s
            WHERE id = %s
        """, (data['teller_number'], data['teller_name'], data.get('email'), 
              data.get('phone'), data['serving_category'], teller_id))
        conn.commit()
        
        return jsonify({'success': True, 'message': 'Teller updated successfully'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/tellers/<int:teller_id>', methods=['DELETE'])
def delete_teller(teller_id):
    """Delete a teller"""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        cursor.execute("DELETE FROM tellers WHERE id = %s", (teller_id,))
        conn.commit()
        
        return jsonify({'success': True, 'message': 'Teller deleted successfully'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/tellers/<int:teller_id>/reset-pin', methods=['POST'])
def reset_teller_pin(teller_id):
    """Reset teller PIN"""
    data = request.get_json()
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        cursor.execute("UPDATE tellers SET pin_code = %s WHERE id = %s", 
                      (data['pin_code'], teller_id))
        conn.commit()
        
        return jsonify({'success': True, 'message': 'PIN reset successfully'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# ============================================
# RUN THE APPLICATION
# ============================================

if __name__ == '__main__':
    print("=" * 50)
    print("🚀 Queue Management System API (Flask)")
    print("=" * 50)
    print(f"📍 API URL: http://localhost:5000")
    print(f"📖 Endpoints:")
    print(f"   GET  /api/health")
    print(f"   POST /api/tokens")
    print(f"   GET  /api/queue/position/<token>")
    print(f"   GET  /api/queue/current")
    print(f"   GET  /api/queue/waiting/<service>")
    print(f"   GET  /api/tellers")
    print(f"   POST /api/tellers/login")
    print(f"   POST /api/tellers")
    print(f"   PUT  /api/tellers/<id>")
    print(f"   DELETE /api/tellers/<id>")
    print(f"   POST /api/tellers/<id>/reset-pin")
    print(f"   POST /api/tellers/call-next")
    print(f"   POST /api/tellers/serve")
    print(f"   POST /api/tellers/complete")
    print(f"   POST /api/tellers/skip")
    print(f"   POST /api/tellers/recall")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=5000, debug=True)