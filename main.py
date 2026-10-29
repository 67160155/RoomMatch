import os
import io
import re
import time
import json
import base64
import requests
from enum import Enum
from typing import Optional, List
from fastapi import FastAPI, HTTPException, status, Depends, UploadFile, File, Header, Form, Request
from pydantic import BaseModel, EmailStr
import psycopg2
from psycopg2.extras import RealDictCursor
from passlib.context import CryptContext
from fastapi.middleware.cors import CORSMiddleware

# AI Libraries (โหลดแบบ Optional เพื่อรองรับ hosting ที่ RAM จำกัด)
CLIP_AVAILABLE = False
model = None
processor = None
DEVICE = "cpu"

try:
    import torch
    import numpy as np
    from PIL import Image
    from transformers import CLIPProcessor, CLIPModel
    DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Loading AI Model on device: {DEVICE}...")
    model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").to(DEVICE)
    processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
    CLIP_AVAILABLE = True
    print(f"CLIP Model loaded successfully on {DEVICE}!")
except Exception as clip_err:
    print(f"[Info] CLIP model not loaded: {clip_err}")
    print("[Info] Main features (AI room design via Ollama) still work.")
    try:
        import torch
        import numpy as np
        from PIL import Image
    except Exception:
        pass

app = FastAPI(title="RoomMatch API")

# อนุญาตให้ Frontend ยิง API ข้ามพอร์ตได้
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://myuser:mypassword@localhost:5432/mydatabase")

# Ollama URL สำหรับ AI Recommendation
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434/api/generate")

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()

# ==========================================
# ตารางบันทึกเหตุการณ์ของสินค้า (product_events)
#   event_type = 'ai_pick'     : AI เลือกสินค้านี้ให้ลูกค้า
#   event_type = 'store_click' : ลูกค้ากดสั่งซื้อ (ไปหน้าร้านค้า)
# ==========================================
PRODUCT_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS product_events (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    event_type VARCHAR(20) NOT NULL,
    user_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_product_events_product_type
    ON product_events (product_id, event_type);
"""

USER_ROOM_DESIGNS_DDL = """
CREATE TABLE IF NOT EXISTS user_room_designs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    username VARCHAR(100),
    room_type VARCHAR(100),
    style VARCHAR(100),
    budget NUMERIC(12, 2),
    image_url TEXT,
    design_data JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_user_room_designs_user ON user_room_designs (user_id);
CREATE INDEX IF NOT EXISTS idx_user_room_designs_created ON user_room_designs (created_at DESC);
"""

@app.on_event("startup")
def ensure_product_events_table():
    # สร้างตารางอัตโนมัติถ้ายังไม่มี (รันซ้ำได้ ไม่ทำลายข้อมูลเดิม)
    # ลองใหม่หลายครั้ง เผื่อฐานข้อมูลยังเริ่มทำงานไม่เสร็จตอน backend ขึ้น
    last_error = None
    for _ in range(15):
        try:
            conn = psycopg2.connect(DATABASE_URL)
            try:
                with conn.cursor() as cur:
                    cur.execute(PRODUCT_EVENTS_DDL)
                    cur.execute(USER_ROOM_DESIGNS_DDL)
                    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT;")
                    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id VARCHAR(100);")
                conn.commit()
            finally:
                conn.close()
            print("product_events and user_room_designs tables are ready")
            return
        except Exception as e:
            last_error = e
            time.sleep(2)
    print(f"Warning: could not create tables: {last_error}")

def parse_optional_user_id(*values) -> Optional[int]:
    for v in values:
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
    return None

def log_product_events(conn, product_ids, event_type: str, user_id: Optional[int] = None):
    """บันทึกเหตุการณ์ ถ้าบันทึกไม่สำเร็จต้องไม่ทำให้ API หลักล้ม"""
    try:
        rows = [(int(pid), event_type, user_id) for pid in product_ids]
        if not rows:
            return
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO product_events (product_id, event_type, user_id) VALUES (%s, %s, %s)",
                rows
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Warning: could not log {event_type}: {e}")

def count_events(conn, seller_name: Optional[str] = None) -> dict:
    """นับเหตุการณ์ทั้งระบบ หรือเฉพาะสินค้าของร้านค้าที่ระบุ"""
    counts = {"ai_pick": 0, "store_click": 0}
    try:
        with conn.cursor() as cursor:
            if seller_name:
                cursor.execute(
                    """
                    SELECT e.event_type, COUNT(*) AS c
                    FROM product_events e
                    JOIN products p ON p.id = e.product_id
                    WHERE p.seller_name = %s
                    GROUP BY e.event_type
                    """,
                    (seller_name,)
                )
            else:
                cursor.execute("SELECT event_type, COUNT(*) AS c FROM product_events GROUP BY event_type")
            for row in cursor.fetchall():
                counts[row['event_type']] = row['c']
    except Exception as e:
        conn.rollback()
        print(f"Warning: could not count events: {e}")
    return counts

def count_events_by_product(conn) -> dict:
    """คืนค่า {product_id: {'ai_pick': n, 'store_click': n}}"""
    result = {}
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT product_id, event_type, COUNT(*) AS c FROM product_events GROUP BY product_id, event_type")
            for row in cursor.fetchall():
                result.setdefault(row['product_id'], {})[row['event_type']] = row['c']
    except Exception as e:
        conn.rollback()
        print(f"Warning: could not count events by product: {e}")
    return result

# กำหนด Enum สำหรับสิทธิ์ผู้ใช้งาน
class UserRole(str, Enum):
    CUSTOMER = "customer"
    SELLER = "seller"
    ADMIN = "admin"

# Schemas
class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str
    role: Optional[UserRole] = UserRole.CUSTOMER 

class UserLogin(BaseModel):
    username: str
    password: str

class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    role: str
    avatar_url: Optional[str] = None

class GoogleAuthRequest(BaseModel):
    credential: Optional[str] = None
    email: Optional[EmailStr] = None
    name: Optional[str] = None
    picture: Optional[str] = None
    role: Optional[UserRole] = UserRole.CUSTOMER

class AdminUserUpdate(BaseModel):
    role: str
    is_active: Optional[bool] = True

class ProductStatusUpdate(BaseModel):
    is_active: bool

class RecommendRequest(BaseModel):
    room_type: str
    style: str
    budget: float
    room_image: Optional[str] = None

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_valid_user_id(x_user_id: Optional[str], user_id: Optional[str]) -> int:
    uid_str = x_user_id or user_id
    if not uid_str:
        raise HTTPException(status_code=401, detail="กรุณาเข้าสู่ระบบ")
    try:
        return int(uid_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="User ID ไม่ถูกต้อง")

@app.get("/")
def read_root():
    return {"message": "Welcome to RoomMatch API"}

@app.post("/register", status_code=status.HTTP_201_CREATED)
def register(user: UserRegister, conn=Depends(get_db)):
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM users WHERE username = %s OR email = %s", (user.username, user.email))
        if cursor.fetchone():
            raise HTTPException(status_code=400, detail="Username หรือ Email นี้ถูกใช้งานแล้ว")
        
        hashed_pwd = hash_password(user.password)
        final_role = user.role.value if user.role else "customer"

        cursor.execute(
            "INSERT INTO users (username, email, password_hash, role) VALUES (%s, %s, %s, %s) RETURNING id, username, email, role",
            (user.username, user.email, hashed_pwd, final_role)
        )
        new_user = cursor.fetchone()
        conn.commit()
        return {"message": "สมัครสมาชิกสำเร็จ", "user": new_user}

@app.post("/login")
def login(user_credentials: UserLogin, conn=Depends(get_db)):
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM users WHERE username = %s", (user_credentials.username,))
        user = cursor.fetchone()
        
        if not user or not verify_password(user_credentials.password, user['password_hash']):
            raise HTTPException(status_code=401, detail="ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
            
        if 'is_active' in user and user['is_active'] == False:
            raise HTTPException(status_code=403, detail="บัญชีนี้ถูกระงับการใช้งาน กรุณาติดต่อผู้ดูแลระบบ")
        
        return {
            "message": "เข้าสู่ระบบสำเร็จ",
            "user": {
                "id": user['id'], 
                "username": user['username'], 
                "email": user['email'],
                "role": user['role'],
                "avatar_url": user.get('avatar_url')
            }
        }

@app.post("/auth/google")
def google_auth(req: GoogleAuthRequest, conn=Depends(get_db)):
    email = None
    name = req.name
    picture = req.picture
    google_id = None

    # 1. หากมี credential (Google ID Token JWT) ตรวจสอบกับ Google tokeninfo API
    if req.credential:
        try:
            res = requests.get(
                f"https://oauth2.googleapis.com/tokeninfo?id_token={req.credential}",
                timeout=5
            )
            if res.status_code == 200:
                token_data = res.json()
                email = token_data.get("email")
                name = token_data.get("name") or token_data.get("given_name") or name
                picture = token_data.get("picture") or picture
                google_id = token_data.get("sub")
            else:
                raise HTTPException(status_code=400, detail="Google Token ไม่ถูกต้องหรือหมดอายุ")
        except requests.RequestException:
            if req.email:
                email = str(req.email)
            else:
                raise HTTPException(status_code=500, detail="ไม่สามารถติดต่อ Google Server เพื่อตรวจสอบ Token ได้")
    elif req.email:
        email = str(req.email)
    else:
        raise HTTPException(status_code=400, detail="กรุณาระบุข้อมูลบัญชี Google หรือ Token")

    if not email:
        raise HTTPException(status_code=400, detail="ไม่พบอีเมลจากบัญชี Google")

    with conn.cursor() as cursor:
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT;")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id VARCHAR(100);")
        conn.commit()

        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()

        if user:
            if user.get('is_active') is False:
                raise HTTPException(status_code=403, detail="บัญชีนี้ถูกระงับการใช้งาน กรุณาติดต่อผู้ดูแลระบบ")

            updates = []
            params = []
            if picture and ('avatar_url' in user and not user['avatar_url']):
                updates.append("avatar_url = %s")
                params.append(picture)
            if google_id and ('google_id' in user and not user['google_id']):
                updates.append("google_id = %s")
                params.append(google_id)
            if updates:
                params.append(user['id'])
                cursor.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = %s", tuple(params))
                conn.commit()
                if picture:
                    user['avatar_url'] = picture
        else:
            # สมัครสมาชิกใหม่อัตโนมัติด้วยบัญชี Google
            raw_name = name or email.split('@')[0]
            clean_name = re.sub(r'[^a-zA-Z0-9_]', '', raw_name.replace(' ', '_').lower())
            if not clean_name:
                clean_name = "google_user"
            clean_name = clean_name[:35]

            candidate = clean_name
            cursor.execute("SELECT id FROM users WHERE username = %s", (candidate,))
            suffix = 1
            while cursor.fetchone():
                suffix_str = str(suffix)
                candidate = f"{clean_name[:35-len(suffix_str)-1]}_{suffix_str}"
                cursor.execute("SELECT id FROM users WHERE username = %s", (candidate,))
                suffix += 1

            dummy_hash = hash_password(f"google_oauth_{email}_{time.time()}")
            final_role = req.role.value if req.role else "customer"

            cursor.execute(
                """
                INSERT INTO users (username, email, password_hash, role, is_active, avatar_url, google_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, username, email, role, avatar_url
                """,
                (candidate, email, dummy_hash, final_role, True, picture, google_id)
            )
            user = cursor.fetchone()
            conn.commit()

        return {
            "message": "เข้าสู่ระบบด้วย Google สำเร็จ",
            "user": {
                "id": user['id'],
                "username": user['username'],
                "email": user['email'],
                "role": user['role'],
                "avatar_url": user.get('avatar_url') or picture
            }
        }

@app.get("/users/{user_id}", response_model=UserResponse)
def get_user_profile(user_id: int, conn=Depends(get_db)):
    with conn.cursor() as cursor:
        cursor.execute("SELECT id, username, email, role FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="ไม่พบข้อมูลผู้ใช้")
        return user


# ==========================================
# APIs สำหรับ Admin
# ==========================================

@app.get("/api/users")
@app.get("/api/admin/users")
def get_all_users(
    x_user_id: Optional[str] = Header(None, alias="x-user-id"),
    user_id: Optional[str] = Header(None, alias="user-id"),
    conn=Depends(get_db)
):
    uid = get_valid_user_id(x_user_id, user_id)
        
    with conn.cursor() as cursor:
        cursor.execute("SELECT role FROM users WHERE id = %s", (uid,))
        admin = cursor.fetchone()
        if not admin or admin['role'] != 'admin':
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึง (เฉพาะ Admin)")
            
        cursor.execute("SELECT id, username, email, role, is_active FROM users ORDER BY id ASC")
        return cursor.fetchall()

@app.put("/api/users/{target_user_id}")
@app.put("/api/admin/users/{target_user_id}")
def update_user_status(
    target_user_id: int,
    update_data: AdminUserUpdate,
    x_user_id: Optional[str] = Header(None, alias="x-user-id"),
    user_id: Optional[str] = Header(None, alias="user-id"),
    conn=Depends(get_db)
):
    uid = get_valid_user_id(x_user_id, user_id)
        
    with conn.cursor() as cursor:
        cursor.execute("SELECT role FROM users WHERE id = %s", (uid,))
        admin = cursor.fetchone()
        if not admin or admin['role'] != 'admin':
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึง")

        if uid == target_user_id:
            raise HTTPException(status_code=400, detail="ไม่สามารถเปลี่ยนสิทธิ์หรือระงับบัญชีตัวเองได้")

        cursor.execute(
            "UPDATE users SET role = %s, is_active = %s WHERE id = %s",
            (update_data.role, update_data.is_active, target_user_id)
        )
        conn.commit()
        return {"message": "อัปเดตข้อมูลผู้ใช้งานสำเร็จ"}

@app.delete("/api/users/{target_user_id}")
@app.delete("/api/admin/users/{target_user_id}")
def delete_user(
    target_user_id: int,
    x_user_id: Optional[str] = Header(None, alias="x-user-id"),
    user_id: Optional[str] = Header(None, alias="user-id"),
    conn=Depends(get_db)
):
    uid = get_valid_user_id(x_user_id, user_id)
        
    with conn.cursor() as cursor:
        cursor.execute("SELECT role FROM users WHERE id = %s", (uid,))
        admin = cursor.fetchone()
        if not admin or admin['role'] != 'admin':
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึง")

        if uid == target_user_id:
            raise HTTPException(status_code=400, detail="ไม่สามารถลบบัญชีตัวเองได้")

        cursor.execute("DELETE FROM users WHERE id = %s", (target_user_id,))
        conn.commit()
        return {"message": "ลบผู้ใช้งานสำเร็จ"}

@app.get("/api/admin/stats")
def get_admin_stats(
    x_user_id: Optional[str] = Header(None, alias="x-user-id"),
    user_id: Optional[str] = Header(None, alias="user-id"),
    conn=Depends(get_db)
):
    uid = get_valid_user_id(x_user_id, user_id)
        
    with conn.cursor() as cursor:
        cursor.execute("SELECT role FROM users WHERE id = %s", (uid,))
        admin = cursor.fetchone()
        if not admin or admin['role'] != 'admin':
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึง (เฉพาะ Admin)")
            
        cursor.execute("SELECT COUNT(*) as total FROM users")
        total_users = cursor.fetchone()['total']
        
        cursor.execute("SELECT role, COUNT(*) as count FROM users GROUP BY role")
        role_counts = {row['role']: row['count'] for row in cursor.fetchall()}
        
        cursor.execute("SELECT COUNT(*) as total FROM products")
        total_products = cursor.fetchone()['total']
        
        event_counts = count_events(conn)

        return {
            "total_users": total_users,
            "total_products": total_products,
            "roles": {
                "customer": role_counts.get("customer", 0),
                "seller": role_counts.get("seller", 0),
                "admin": role_counts.get("admin", 0)
            },
            "ai_usage": event_counts["ai_pick"],
            "store_clicks": event_counts["store_click"]
        }


# ==========================================
# APIs สำหรับจัดการสินค้า (Products)
# ==========================================

@app.get("/products")
@app.get("/api/products")
def get_products(
    request: Request,
    x_user_id: Optional[str] = Header(None, alias="x-user-id"),
    user_id: Optional[str] = Header(None, alias="user-id"),
    conn=Depends(get_db)
):
    with conn.cursor() as cursor:
        uid_str = x_user_id or user_id
        
        query = "SELECT id, name, category, price, description, image_data, style_tags, seller_name, image_url, purchase_url AS product_url, is_active, created_at FROM products WHERE is_active = TRUE ORDER BY id DESC"
        params = ()

        if uid_str:
            try:
                uid = int(uid_str)
                cursor.execute("SELECT username, role FROM users WHERE id = %s", (uid,))
                user = cursor.fetchone()
                
                if user:
                    if user['role'] == 'seller':
                        query = "SELECT id, name, category, price, description, image_data, style_tags, seller_name, image_url, purchase_url AS product_url, is_active, created_at FROM products WHERE seller_name = %s ORDER BY id DESC"
                        params = (user['username'],)
                    elif user['role'] == 'admin':
                        query = "SELECT id, name, category, price, description, image_data, style_tags, seller_name, image_url, purchase_url AS product_url, is_active, created_at FROM products ORDER BY id DESC"
                        params = ()
            except ValueError:
                pass 

        cursor.execute(query, params)
        rows = cursor.fetchall()
        event_stats = count_events_by_product(conn)
        
        products = []
        for row in rows:
            product = dict(row)
            stats = event_stats.get(product['id'], {})
            product['ai_usage'] = stats.get('ai_pick', 0)
            product['store_clicks'] = stats.get('store_click', 0)
            if product.get('image_data'):
                image_bytes = product['image_data']
                if isinstance(image_bytes, memoryview):
                    image_bytes = image_bytes.tobytes()
                encoded = base64.b64encode(image_bytes).decode('utf-8')
                product['image_base64'] = f"data:image/jpeg;base64,{encoded}"
            else:
                product['image_base64'] = None
                
            if 'image_data' in product:
                del product['image_data']
            products.append(product)
            
        return products

@app.post("/products")
@app.post("/api/products")
async def create_product(
    name: str = Form(...),
    category: str = Form(...),
    price: float = Form(...),
    description: Optional[str] = Form(""),
    product_url: Optional[str] = Form(""),
    style_tags: Optional[str] = Form(""), 
    image: Optional[UploadFile] = File(None), 
    image_url: Optional[str] = Form(""), 
    x_user_id: Optional[str] = Header(None, alias="x-user-id"),
    user_id: Optional[str] = Header(None, alias="user-id"),
    conn=Depends(get_db)
):
    uid = get_valid_user_id(x_user_id, user_id)

    with conn.cursor() as cursor:
        cursor.execute("SELECT id, username, role FROM users WHERE id = %s", (uid,))
        user = cursor.fetchone()
        
        if not user:
            raise HTTPException(status_code=404, detail="ไม่พบข้อมูลผู้ใช้งาน")
            
        if user['role'] not in ['seller', 'admin']:
            raise HTTPException(status_code=403, detail="เฉพาะบัญชีร้านค้า (Seller) หรือ Admin เท่านั้นที่สามารถเพิ่มสินค้าได้")
            
        seller_name = user['username']
        
        try:
            image_bytes = None
            
            # 1. จัดการรูปภาพ (ตรวจสอบจากไฟล์อัปโหลดก่อน ถัดไปเป็น URL)
            if image and image.filename:
                image_bytes = await image.read()
            elif image_url and image_url.startswith("http"):
                try:
                    resp = requests.get(image_url, timeout=5)
                    if resp.status_code == 200:
                        image_bytes = resp.content
                except Exception as e:
                    print(f"Failed to fetch image from URL: {e}")
            
            if not image_bytes:
                raise HTTPException(status_code=400, detail="กรุณาอัปโหลดรูปภาพหรือระบุ URL รูปภาพที่สามารถเข้าถึงได้")

            # 2. แปลงรูปภาพและคำนวณ Embedding Vector ด้วย CLIP Model
            if not CLIP_AVAILABLE:
                raise HTTPException(status_code=503, detail="CLIP Model ไม่พร้อมใช้งานในโหมด Cloud (RAM จำกัด) กรุณาเพิ่มสินค้าโดยไม่ใช้ Image Embedding")
            pil_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            inputs = processor(images=pil_image, return_tensors="pt")
            inputs = {k: v.to(DEVICE) for k, v in inputs.items()}
            
            with torch.no_grad():
                outputs = model.get_image_features(**inputs)
                if not isinstance(outputs, torch.Tensor):
                    if hasattr(outputs, 'image_embeds'):
                        image_features = outputs.image_embeds
                    elif hasattr(outputs, 'pooler_output'):
                        image_features = outputs.pooler_output
                    else:
                        image_features = outputs[0]
                else:
                    image_features = outputs
            
            image_features = image_features / image_features.norm(p=2, dim=-1, keepdim=True)
            image_vector = image_features[0].tolist()
            image_vector_str = str(image_vector)

            # 3. บันทึกลง Database
            cursor.execute(
                """
                INSERT INTO products (name, category, price, description, image_data, seller_name, image_embedding, image_url, purchase_url, style_tags, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, TRUE)
                RETURNING id, name, category, price, description, seller_name, purchase_url AS product_url, style_tags, is_active, created_at
                """,
                (
                    name, 
                    category, 
                    price, 
                    description, 
                    psycopg2.Binary(image_bytes) if (image and image.filename) else None, 
                    seller_name, 
                    image_vector_str, 
                    image_url if not (image and image.filename) else "", 
                    product_url, 
                    style_tags
                )
            )
            new_product = dict(cursor.fetchone())
            conn.commit()
            
            return {"message": "เพิ่มสินค้าลงในระบบสำเร็จ", "product": new_product}
            
        except HTTPException:
            raise
        except Exception as e:
            conn.rollback()
            print(f"Error creating product: {str(e)}")
            raise HTTPException(status_code=500, detail=f"เกิดข้อผิดพลาดภายในระบบ: {str(e)}")

@app.patch("/products/{product_id}/status")
@app.patch("/api/products/{product_id}/status")
def toggle_product_status(
    product_id: int,
    status_data: ProductStatusUpdate,
    x_user_id: Optional[str] = Header(None, alias="x-user-id"),
    user_id: Optional[str] = Header(None, alias="user-id"),
    conn=Depends(get_db)
):
    uid = get_valid_user_id(x_user_id, user_id)

    with conn.cursor() as cursor:
        cursor.execute("SELECT username, role FROM users WHERE id = %s", (uid,))
        user = cursor.fetchone()
        if not user or user['role'] not in ['seller', 'admin']:
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์แก้ไขสินค้านี้")

        if user['role'] == 'admin':
            cursor.execute(
                "UPDATE products SET is_active = %s WHERE id = %s",
                (status_data.is_active, product_id)
            )
        else:
            cursor.execute(
                "UPDATE products SET is_active = %s WHERE id = %s AND seller_name = %s",
                (status_data.is_active, product_id, user['username'])
            )

        conn.commit()
        return {"message": "อัปเดตสถานะสินค้าเรียบร้อยแล้ว"}

@app.delete("/products/{product_id}")
@app.delete("/api/products/{product_id}")
def delete_product(
    product_id: int, 
    x_user_id: Optional[str] = Header(None, alias="x-user-id"),
    user_id: Optional[str] = Header(None, alias="user-id"),
    conn=Depends(get_db)
):
    uid = get_valid_user_id(x_user_id, user_id)

    with conn.cursor() as cursor:
        cursor.execute("SELECT id, username, role FROM users WHERE id = %s", (uid,))
        user = cursor.fetchone()
        
        if not user:
            raise HTTPException(status_code=404, detail="ไม่พบข้อมูลผู้ใช้งาน")
            
        if user['role'] not in ['seller', 'admin']:
            raise HTTPException(status_code=403, detail="เฉพาะบัญชีร้านค้า (Seller) หรือ Admin เท่านั้นที่สามารถลบสินค้าได้")

        if user['role'] == 'admin':
            cursor.execute("SELECT * FROM products WHERE id = %s", (product_id,))
        else:
            cursor.execute("SELECT * FROM products WHERE id = %s AND seller_name = %s", (product_id, user['username']))

        product = cursor.fetchone()
        if not product:
            raise HTTPException(status_code=404, detail="ไม่พบสินค้า หรือคุณไม่มีสิทธิ์ลบสินค้านี้")

        cursor.execute("DELETE FROM products WHERE id = %s", (product_id,))
        conn.commit()

        return {"message": "ลบสินค้าสำเร็จ"}

@app.put("/products/{product_id}")
@app.put("/api/products/{product_id}")
def update_product(
    product_id: int,
    name: str = Form(...),
    category: str = Form(...),
    price: float = Form(...),
    description: Optional[str] = Form(""),
    product_url: Optional[str] = Form(""),
    x_user_id: Optional[str] = Header(None, alias="x-user-id"),
    user_id: Optional[str] = Header(None, alias="user-id"),
    conn=Depends(get_db)
):
    uid = get_valid_user_id(x_user_id, user_id)
        
    with conn.cursor() as cursor:
        cursor.execute("SELECT username, role FROM users WHERE id = %s", (uid,))
        user = cursor.fetchone()
        if not user or user['role'] not in ['seller', 'admin']:
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึง (เฉพาะผู้ขายและ Admin)")
            
        if user['role'] == 'admin':
            cursor.execute(
                """
                UPDATE products 
                SET name = %s, category = %s, price = %s, description = %s, purchase_url = %s 
                WHERE id = %s
                """,
                (name, category, price, description, product_url, product_id)
            )
        else:
            cursor.execute(
                """
                UPDATE products 
                SET name = %s, category = %s, price = %s, description = %s, purchase_url = %s 
                WHERE id = %s AND seller_name = %s
                """,
                (name, category, price, description, product_url, product_id, user['username'])
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="ไม่พบสินค้า หรือไม่ใช่สินค้าของร้านคุณ")

        conn.commit()
        return {"message": "แก้ไขข้อมูลสินค้าสำเร็จ"}

# API สำหรับค้นหาเฟอร์นิเจอร์จากรูปภาพห้อง (AI Vector Search)
@app.post("/api/match-room")
async def match_room(
    file: UploadFile = File(...), 
    budget: Optional[str] = Form(None), 
    conn=Depends(get_db)
):
    try:
        # 1. จัดการค่า Budget อย่างปลอดภัย
        parsed_budget = None
        if budget:
            clean_budget = budget.strip().replace(",", "")
            if clean_budget.lower() not in ["", "null", "undefined", "none"]:
                try:
                    parsed_budget = float(clean_budget)
                except ValueError:
                    pass 

        # 2. จัดการรูปภาพและ AI
        if not CLIP_AVAILABLE:
            raise HTTPException(status_code=503, detail="ระบบค้นหาสินค้าด้วยรูปภาพ (CLIP) ไม่พร้อมในโหมด Cloud ใช้ฟีเจอร์ 'ออกแบบห้อง' แทนได้ครับ")
        image_bytes = await file.read()
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        inputs = processor(images=image, return_tensors="pt")
        inputs = {k: v.to(DEVICE) for k, v in inputs.items()}
        
        with torch.no_grad():
            outputs = model.get_image_features(**inputs)
            if not isinstance(outputs, torch.Tensor):
                if hasattr(outputs, 'image_embeds'):
                    image_features = outputs.image_embeds
                elif hasattr(outputs, 'pooler_output'):
                    image_features = outputs.pooler_output
                else:
                    image_features = outputs[0]
            else:
                image_features = outputs
        
        image_features = image_features / image_features.norm(p=2, dim=-1, keepdim=True)
        room_vector = image_features[0].tolist()
        room_vector_str = str(room_vector)

        # 3. ค้นหาในฐานข้อมูลและควบคุมราคารวม
        with conn.cursor() as cursor:
            if parsed_budget is not None and parsed_budget > 0:
                # ดึงมาเผื่อ 15 ชิ้นก่อน
                query = """
                SELECT id, name, category, price, description, image_data, style_tags, seller_name, purchase_url AS product_url,
                       1 - (image_embedding <=> %s::vector) AS similarity_score
                FROM products
                WHERE is_active = TRUE AND price <= %s
                ORDER BY image_embedding <=> %s::vector
                LIMIT 15;
                """
                cursor.execute(query, (room_vector_str, parsed_budget, room_vector_str))
                rows = cursor.fetchall()
                
                results = []
                current_total_price = 0.0
                
                for row in rows:
                    product = dict(row)
                    item_price = float(product['price'])
                    
                    # ถ้ารวมชิ้นนี้เข้าไปแล้ว "ราคารวม" ยังไม่เกินงบ ให้หยิบใส่ตะกร้า
                    if current_total_price + item_price <= parsed_budget:
                        if product.get('image_data'):
                            img_bytes = product['image_data']
                            if isinstance(img_bytes, memoryview):
                                img_bytes = img_bytes.tobytes()
                            encoded = base64.b64encode(img_bytes).decode('utf-8')
                            product['image_base64'] = f"data:image/jpeg;base64,{encoded}"
                        else:
                            product['image_base64'] = None
                            
                        if 'image_data' in product:
                            del product['image_data']
                            
                        results.append(product)
                        current_total_price += item_price # บวกราคารวมสะสม
                        
                    # ถ้าได้ครบ 4 ชิ้นแล้วให้หยุดทันที
                    if len(results) >= 4:
                        break
            else:
                # กรณีไม่ได้ระบุงบ ดึง 4 ชิ้นปกติ
                query = """
                SELECT id, name, category, price, description, image_data, style_tags, seller_name, purchase_url AS product_url,
                       1 - (image_embedding <=> %s::vector) AS similarity_score
                FROM products
                WHERE is_active = TRUE
                ORDER BY image_embedding <=> %s::vector
                LIMIT 4;
                """
                cursor.execute(query, (room_vector_str, room_vector_str))
                rows = cursor.fetchall()
                
                results = []
                for row in rows:
                    product = dict(row)
                    if product.get('image_data'):
                        img_bytes = product['image_data']
                        if isinstance(img_bytes, memoryview):
                            img_bytes = img_bytes.tobytes()
                        encoded = base64.b64encode(img_bytes).decode('utf-8')
                        product['image_base64'] = f"data:image/jpeg;base64,{encoded}"
                    else:
                        product['image_base64'] = None
                        
                    if 'image_data' in product:
                        del product['image_data']
                    results.append(product)
            
            log_product_events(conn, [p['id'] for p in results], 'ai_pick')
            return {"status": "success", "data": results, "total_price": sum(float(p['price']) for p in results)}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# หมวดสินค้าที่เหมาะกับแต่ละประเภทห้อง
# (ชื่อหมวดตรงกับปุ่มกรองในหน้า result.html และค่าที่ผู้ขายกรอกตอนเพิ่มสินค้า)
# ==========================================
ROOM_CATEGORY_RULES = [
    # (คำที่พบในชื่อประเภทห้อง, ชื่อห้องสำหรับพรอมป์ต์, หมวดที่อนุญาต)
    (("นอน", "bedroom"),
     "ห้องนอน", ["เตียง", "ตู้", "โต๊ะ", "ของตกแต่ง", "อื่นๆ"]),
    (("รับแขก", "นั่งเล่น", "living"),
     "ห้องรับแขก/ห้องนั่งเล่น", ["โซฟา", "โต๊ะ", "ตู้", "เก้าอี้", "ของตกแต่ง", "อื่นๆ"]),
    (("ทำงาน", "working", "office"),
     "ห้องทำงาน", ["โต๊ะ", "เก้าอี้", "ตู้", "ของตกแต่ง", "อื่นๆ"]),
    (("ครัว", "kitchen"),
     "ห้องครัว", ["โต๊ะ", "เก้าอี้", "ตู้", "ของตกแต่ง", "อื่นๆ"]),
]

def allowed_categories_for_room(room_type: str) -> Optional[List[str]]:
    """คืนรายการหมวดที่เหมาะกับห้อง หรือ None ถ้าไม่รู้จักประเภทห้อง (ไม่กรอง)"""
    text = (room_type or "").lower()
    for keywords, _label, categories in ROOM_CATEGORY_RULES:
        if any(k in text for k in keywords):
            return categories
    return None

def filter_products_by_room(products, room_type: str):
    """กรองสินค้าตามหมวดที่เหมาะกับห้อง (ป้องกันการนำสินค้าผิดหมวดข้ามห้อง เช่น ห้ามนำเตียงไปใส่ห้องรับแขก)"""
    allowed = allowed_categories_for_room(room_type)
    if not allowed:
        return products, False
    filtered = [p for p in products if any(a in (p['category'] or "") for a in allowed)]
    # คืนเฉพาะสินค้าที่ตรงหมวดเท่านั้น หากไม่มีสินค้าตรงหมวด ให้คืน [] เพื่อแจ้งเตือนให้ผู้ใช้เปิดสินค้าในหมวดที่ถูกต้อง
    return filtered, True


# ==========================================
# ตัวช่วยอ่านผลจาก AI ให้ทนต่อการตอบผิดรูปแบบ
# ==========================================
def resolve_selected_ids(raw_items, products) -> List[int]:
    """แปลงค่าใน selected_ids ที่ AI ตอบ ให้เป็น id ที่มีอยู่จริงในรายการสินค้า
    รับได้: ตัวเลข, ตัวเลขแบบข้อความ ("7"), object ({"id": 7}) และชื่อสินค้า"""
    by_id = {p['id']: p for p in products}
    if raw_items is None or raw_items == "":
        return []
    if not isinstance(raw_items, list):
        raw_items = [raw_items]

    result: List[int] = []
    for item in raw_items:
        if isinstance(item, dict):
            item = item.get("id", item.get("product_id", item.get("name")))
        if isinstance(item, bool):
            continue

        pid = None
        if isinstance(item, (int, float)):
            if float(item).is_integer():
                pid = int(item)
        elif isinstance(item, str):
            s = item.strip()
            if s.isdigit():
                pid = int(s)
            elif len(s) >= 8:  # จับคู่ด้วยชื่อสินค้า (ต้องยาวพอ และตรงกับสินค้าเพียงชิ้นเดียวเท่านั้น)
                matches = []
                for p in products:
                    name = (p['name'] or "").strip()
                    if name and (s == name or s in name or name in s):
                        matches.append(p['id'])
                if len(matches) == 1:
                    pid = matches[0]

        if pid in by_id and pid not in result:
            result.append(pid)
    return result

def fallback_pick_products(products, room_type: str, budget: float, limit: int = 3) -> List[int]:
    """เลือกสินค้าเองเมื่อ AI ตอบ id ไม่ได้: หมวดที่ห้องนั้นต้องการมาก่อน หมวดละ 1 ชิ้น ราคาถูกก่อน และรวมไม่เกินงบ"""
    allowed = allowed_categories_for_room(room_type) or []

    def rank(p):
        cat = p['category'] or ""
        for i, a in enumerate(allowed):
            if a in cat:
                return i
        return len(allowed)

    picked: List[int] = []
    seen_cats = set()
    total = 0.0
    for p in sorted(products, key=lambda x: (rank(x), float(x['price']))):
        cat = p['category'] or ""
        price = float(p['price'])
        if cat in seen_cats or total + price > budget:
            continue
        picked.append(p['id'])
        seen_cats.add(cat)
        total += price
        if len(picked) >= limit:
            break
    return picked

def parse_reasons(parsed: dict, products) -> dict:
    """อ่านเหตุผลรายชิ้นที่ AI ตอบ -> {product_id: reason}"""
    reasons = {}
    raw = parsed.get("reasons")
    pairs = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                pairs.append((item.get("id", item.get("name")), item.get("reason")))
    elif isinstance(raw, dict):
        pairs = list(raw.items())
    for key, reason in pairs:
        ids = resolve_selected_ids([key], products)
        if ids and isinstance(reason, str) and reason.strip():
            reasons[ids[0]] = reason.strip()
    return reasons

# ลบการพูดถึงราคาที่ AI เขียนเอง (ราคาจริงจะแสดงจากสินค้าจริงแทน)
_PRICE_PATTERNS = [
    re.compile(r"(?:ซึ่ง|โดย)?(?:รวม)?(?:เป็น)?(?:มี)?ราคา(?:รวม)?(?:เป็น)?\s*[\d,]+(?:\.\d+)?\s*บาท"),
    re.compile(r"(?:ซึ่ง)?(?:ไม่เกิน|เกิน|อยู่ในงบ)\s*[\d,]+(?:\.\d+)?\s*บาท"),
    re.compile(r"\(?\s*[\d,]+(?:\.\d+)?\s*บาท\s*\)?"),
]

def strip_price_mentions(text: str) -> str:
    text = text or ""
    for pat in _PRICE_PATTERNS:
        text = pat.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip(" ,;:—-")

def compose_recommendation(overview, products, reasons, room_type, budget, trimmed, used_fallback) -> str:
    """ประกอบข้อความคำแนะนำจากสินค้าจริง ชื่อและราคาตรงกับการ์ดที่แสดง"""
    parts = []
    if used_fallback:
        parts.append(f"ระบบคัดสินค้าที่เหมาะกับ{room_type}และอยู่ในงบประมาณของคุณให้แล้ว")
    else:
        overview = strip_price_mentions(str(overview or ""))
        if overview:
            parts.append(overview)

    if products:
        lines = ["สินค้าที่แนะนำ:"]
        for p in products:
            line = f"• {p['name']} ({p['price']:,.0f} บาท)"
            reason = strip_price_mentions(reasons.get(p['id'], ""))
            if reason:
                line += f" — {reason}"
            lines.append(line)
        parts.append("\n".join(lines))

    if trimmed:
        parts.append("(หมายเหตุ: ระบบตัดสินค้าบางรายการออกเพื่อไม่ให้เกินงบของคุณ)")

    total = sum(p['price'] for p in products)
    parts.append(f"ราคารวมสินค้าที่แนะนำ: {total:,.0f} บาท (งบประมาณ {budget:,.0f} บาท)")
    return "\n\n".join(parts)


def detect_existing_furniture(img: Image.Image) -> List[str]:
    """ใช้โมเดล CLIP AI ตรวจจับว่าในภาพห้องมีเฟอร์นิเจอร์หลักชิ้นใดอยู่แล้วบ้าง เพื่อป้องกันการแนะนำซ้ำซ้อน"""
    if not CLIP_AVAILABLE:
        return []
    try:
        furniture_checks = {
            'เตียง': ['a bedroom with an existing bed', 'a photo of a bed in a room'],
            'โซฟา': ['a living room with an existing sofa or couch', 'a photo of a sofa couch in the room'],
            'โต๊ะ': ['a room with an existing table or desk', 'a photo of a dining table or work desk in the room'],
            'ตู้': ['a room with an existing wardrobe or large cabinet', 'a photo of a wardrobe in the room']
        }
        empty_prompt = 'an empty room with bare floor and no furniture'
        all_prompts = [empty_prompt]
        prompt_cat_map = [None]
        for cat, prompts in furniture_checks.items():
            for p in prompts:
                all_prompts.append(p)
                prompt_cat_map.append(cat)

        inputs = processor(text=all_prompts, images=img, return_tensors='pt', padding=True)
        inputs = {k: v.to(DEVICE) for k, v in inputs.items()}
        with torch.no_grad():
            outputs = model(**inputs)
            probs = outputs.logits_per_image.softmax(dim=1)[0].tolist()

        empty_score = probs[0]
        detected = {}
        for idx in range(1, len(all_prompts)):
            cat = prompt_cat_map[idx]
            score = probs[idx]
            if cat not in detected or score > detected[cat]:
                detected[cat] = score

        # ถ้าคะแนนห้องเปล่าสูงเกิน 60% ถือว่าเป็นห้องเปล่า
        if empty_score > 0.60:
            return []

        # หมวดที่โมเดลมั่นใจว่ามีอยู่แล้ว (คะแนนเกิน 20% และมากกว่าห้องเปล่า)
        existing = [cat for cat, score in detected.items() if score > 0.20 and score > empty_score]
        return existing
    except Exception as e:
        print(f"Warning: detect_existing_furniture error: {e}")
        return []


def analyze_room_image(image_data_or_base64) -> dict:
    """วิเคราะห์องค์ประกอบภาพห้องจริงด้วย AI Vision: ตรวจสอบเฟอร์นิเจอร์เดิม ระนาบพื้น และทิศทางแสงธรรมชาติ"""
    default_info = {
        'floor_baseline_y': 66,
        'window_side': 'right',
        'window_side_th': 'ขวา',
        'anchor_wall_th': 'ซ้ายและกึ่งกลาง',
        'light_analysis': 'ตรวจพบช่องรับแสงธรรมชาติที่เปิดความโปร่งโล่งให้ห้อง',
        'wall_analysis': 'ผนังทึบหลักเหมาะสำหรับเป็นจุดยึดสายตา (Anchor Point) ในการวางเฟอร์นิเจอร์ชิ้นหลัก',
        'floor_analysis': 'ระนาบพื้นห้องกว้างขวาง เหมาะสำหรับการจัดวางแบบเปิดทางสัญจร (Open Flow Circulation)',
        'existing_furniture': [],
        'has_existing_furniture': False,
        'existing_summary': 'ตรวจพบว่าเป็นห้องเปล่า ยังไม่มีเฟอร์นิเจอร์หลัก'
    }
    if not image_data_or_base64:
        return default_info
    try:
        raw_str = str(image_data_or_base64)
        if ',' in raw_str:
            raw_str = raw_str.split(',', 1)[1]
        raw_bytes = base64.b64decode(raw_str)
        img = Image.open(io.BytesIO(raw_bytes)).convert('RGB')
        
        # 0. ตรวจจับเฟอร์นิเจอร์เดิมในห้องด้วย OpenAI CLIP (เช่น มีเตียงอยู่แล้ว หรือมีโซฟาอยู่แล้ว)
        existing_items = detect_existing_furniture(img)
        existing_desc = f"ตรวจพบว่าห้องนี้มี {', '.join(existing_items)} อยู่แล้ว" if existing_items else "ตรวจพบว่าเป็นห้องเปล่า ยังไม่มีเฟอร์นิเจอร์หลัก"
        
        # ย่อขนาดเพื่อวิเคราะห์มิติภาพอย่างรวดเร็วระดับมิลลิวินาที
        img_small = img.resize((160, 160))
        arr = np.array(img_small, dtype=float)
        
        # 1. ตรวจสอบแนวระนาบพื้นห้องจริง (Floor Baseline)
        diffs = []
        for y_pct in range(48, 80):
            y = int(160 * y_pct / 100)
            row_cur = np.mean(arr[y:y+3, :], axis=(0,1))
            row_next = np.mean(arr[y+5:y+8, :], axis=(0,1))
            diffs.append((y_pct, float(np.linalg.norm(row_cur - row_next))))
        diffs.sort(key=lambda x: x[1], reverse=True)
        detected_floor = diffs[0][0] if diffs else 66
        detected_floor = max(55, min(75, detected_floor))
        
        # 2. ตรวจสอบทิศทางหน้าต่าง/ช่องแสงธรรมชาติในโซนผนัง (y: 20% ถึง 62%)
        wall_zone = arr[int(160*0.2):int(160*0.62), :]
        left_bright = float(np.mean(wall_zone[:, :int(160*0.35)]))
        right_bright = float(np.mean(wall_zone[:, int(160*0.65):]))
        
        right_var = float(np.std(wall_zone[:, int(160*0.7):]))
        left_var = float(np.std(wall_zone[:, :int(160*0.3)]))
        
        if right_var > left_var + 6 or right_bright > left_bright + 12:
            win_side = 'right'
            win_th = 'ขวา'
            anchor_th = 'ซ้ายและกึ่งกลาง'
            light_desc = 'ตรวจพบช่องแสงธรรมชาติจากหน้าต่างฝั่งขวาของห้อง ช่วยให้บรรยากาศสว่างและโปร่งสบายตา'
            wall_desc = 'ผนังทึบฝั่งซ้ายและกลางห้องเป็นจุดยึดสายตาหลัก (Anchor Wall) เหมาะสำหรับวางเฟอร์นิเจอร์ชิ้นใหญ่'
        elif left_var > right_var + 6 or left_bright > right_bright + 12:
            win_side = 'left'
            win_th = 'ซ้าย'
            anchor_th = 'ขวาและกึ่งกลาง'
            light_desc = 'ตรวจพบช่องแสงธรรมชาติจากหน้าต่างฝั่งซ้ายของห้อง ส่องแสงเข้ามาภายใน'
            wall_desc = 'ผนังทึบฝั่งขวาและกลางห้องเป็นจุดยึดสายตาหลัก (Anchor Wall) เหมาะสำหรับวางเฟอร์นิเจอร์หลัก'
        else:
            win_side = 'center'
            win_th = 'สองฝั่ง/ทั่วถึง'
            anchor_th = 'ผนังหลักตรงกลาง'
            light_desc = 'การกระจายแสงธรรมชาติภายในห้องสม่ำเสมอทั่วทั้งพื้นที่'
            wall_desc = 'ผนังหลักตรงกลางห้องเหมาะเป็นจุดนำสายตาที่สมดุล'
            
        return {
            'floor_baseline_y': detected_floor,
            'window_side': win_side,
            'window_side_th': win_th,
            'anchor_wall_th': anchor_th,
            'light_analysis': light_desc,
            'wall_analysis': wall_desc,
            'floor_analysis': f'พื้นห้องเริ่มต้นที่ระดับความสูง {detected_floor}% ของภาพ รองรับการจัดวางแบบเปิดกว้าง (Open Circulation)',
            'existing_furniture': existing_items,
            'has_existing_furniture': len(existing_items) > 0,
            'existing_summary': existing_desc
        }
    except Exception as e:
        print(f"Warning: analyze_room_image failed: {e}")
        return default_info
        print(f"Warning: analyze_room_image failed: {e}")
        return default_info


def compute_spatial_placements(products: list, room_type: str, visual_info: Optional[dict] = None, ai_placements: Optional[list] = None) -> list:
    """คำนวณพิกัดตำแหน่ง (X%, Y%) โซนการออกแบบ และมิติพื้นที่จัดวางเฟอร์นิเจอร์บนภาพห้องจริง"""
    v_info = visual_info or {
        'floor_baseline_y': 66,
        'window_side': 'right',
        'window_side_th': 'ขวา'
    }
    baseline_y = v_info.get('floor_baseline_y', 66)
    win_side = v_info.get('window_side', 'right')

    if win_side == 'right':
        living_presets = [
            {
                "cat": "โซฟา",
                "zone_name": "โซนนั่งเล่นหลัก (Main Living Anchor)",
                "title": "ผนังทึบฝั่งซ้าย-กลางห้อง",
                "x": 36, "y": min(88, baseline_y + 7),
                "tip": "จัดวางโซฟาชิดผนังทึบเพื่อเป็นจุดยึดสายตาหลัก (Anchor Point) เปิดมุมมองทางขวาเพื่อรับแสงธรรมชาติจากหน้าต่าง และทำให้ทางเดินในห้องโล่งโปร่ง",
                "footprint_w": 28, "footprint_h": 12, "icon": "fa-couch", "color": "#2563eb"
            },
            {
                "cat": "โต๊ะ",
                "zone_name": "โซนโต๊ะกลางฟังก์ชัน (Center Core)",
                "title": "กึ่งกลางด้านหน้าโซฟา",
                "x": 54, "y": min(92, baseline_y + 16),
                "tip": "วางโต๊ะกลางเว้นระยะห่างจากโซฟาประมาณ 45 ซม. เป็นระยะสรีรศาสตร์ที่เดินผ่านได้คล่องตัวและเอื้อมหยิบของได้สะดวก",
                "footprint_w": 20, "footprint_h": 9, "icon": "fa-table", "color": "#f59e0b"
            },
            {
                "cat": "เก้าอี้",
                "zone_name": "มุมพักผ่อนรับแสงธรรมชาติ (Sunlit Nook)",
                "title": "ริมหน้าต่างฝั่งขวา",
                "x": 78, "y": min(86, baseline_y + 6),
                "tip": "จัดวางเก้าอี้ริมหน้าต่างฝั่งขวาเพื่อรับแสงธรรมชาติ เหมาะสำหรับนั่งอ่านหนังสือหรือผ่อนคลาย และทำมุมสนทนา 45 องศากับโซฟาหลัก",
                "footprint_w": 16, "footprint_h": 8, "icon": "fa-chair", "color": "#10b981"
            },
            {
                "cat": "ตู้",
                "zone_name": "โซนจัดเก็บและวางมีเดีย (Media Wall)",
                "title": "ผนังด้านซ้ายสุด",
                "x": 18, "y": min(82, baseline_y + 4),
                "tip": "วางชิดผนังด้านซ้ายเพื่อความเป็นระเบียบ ไม่บดบังทางสัญจร และอยู่ในระดับสายตาที่มองเห็นสะดวกจากโซฟา",
                "footprint_w": 22, "footprint_h": 9, "icon": "fa-box-archive", "color": "#8b5cf6"
            },
            {
                "cat": "ของตกแต่ง",
                "zone_name": "มุมสร้างบรรยากาศธรรมชาติ (Green Corner)",
                "title": "มุมห้องริมหน้าต่าง",
                "x": 86, "y": min(85, baseline_y + 8),
                "tip": "วางต้นไม้ฟอกอากาศหรือของตกแต่งใกล้หน้าต่างเพื่อรับแสงแดด เพิ่มความมีชีวิตชีวาและความสดชื่นให้กับห้อง",
                "footprint_w": 14, "footprint_h": 7, "icon": "fa-plant-wilt", "color": "#06b6d4"
            }
        ]
    elif win_side == 'left':
        living_presets = [
            {
                "cat": "โซฟา",
                "zone_name": "โซนนั่งเล่นหลัก (Main Living Anchor)",
                "title": "ผนังทึบฝั่งขวา-กลางห้อง",
                "x": 64, "y": min(88, baseline_y + 7),
                "tip": "จัดวางโซฟาชิดผนังทึบฝั่งขวาเพื่อเป็นจุดยึดสายตาหลัก เปิดมุมมองรับแสงธรรมชาติจากหน้าต่างฝั่งซ้าย",
                "footprint_w": 28, "footprint_h": 12, "icon": "fa-couch", "color": "#2563eb"
            },
            {
                "cat": "โต๊ะ",
                "zone_name": "โซนโต๊ะกลางฟังก์ชัน (Center Core)",
                "title": "กึ่งกลางด้านหน้าโซฟา",
                "x": 48, "y": min(92, baseline_y + 16),
                "tip": "วางโต๊ะกลางกึ่งกลางหน้าโซฟา เว้นระยะเดินผ่านได้สะดวก",
                "footprint_w": 20, "footprint_h": 9, "icon": "fa-table", "color": "#f59e0b"
            },
            {
                "cat": "เก้าอี้",
                "zone_name": "มุมพักผ่อนรับแสงธรรมชาติ (Sunlit Nook)",
                "title": "ริมหน้าต่างฝั่งซ้าย",
                "x": 22, "y": min(86, baseline_y + 6),
                "tip": "จัดวางเก้าอี้ริมหน้าต่างฝั่งซ้ายเพื่อรับแสงธรรมชาติ เหมาะกับการอ่านหนังสือ",
                "footprint_w": 16, "footprint_h": 8, "icon": "fa-chair", "color": "#10b981"
            },
            {
                "cat": "ตู้",
                "zone_name": "โซนจัดเก็บและวางมีเดีย (Media Wall)",
                "title": "ผนังด้านขวาสุด",
                "x": 82, "y": min(82, baseline_y + 4),
                "tip": "วางชิดผนังด้านขวาเพื่อความเป็นระเบียบ",
                "footprint_w": 22, "footprint_h": 9, "icon": "fa-box-archive", "color": "#8b5cf6"
            },
            {
                "cat": "ของตกแต่ง",
                "zone_name": "มุมสร้างบรรยากาศธรรมชาติ (Green Corner)",
                "title": "มุมห้องริมหน้าต่างซ้าย",
                "x": 14, "y": min(85, baseline_y + 8),
                "tip": "วางต้นไม้หรือของตกแต่งริมหน้าต่างเพื่อรับแสง",
                "footprint_w": 14, "footprint_h": 7, "icon": "fa-plant-wilt", "color": "#06b6d4"
            }
        ]
    else:
        living_presets = [
            {
                "cat": "โซฟา", "zone_name": "โซนนั่งเล่นหลัก", "title": "ผนังหลักตรงกลางห้อง",
                "x": 48, "y": min(88, baseline_y + 7), "tip": "จัดวางโซฟาตรงกลางผนังหลักสร้างความสมดุลและความโอ่โถง",
                "footprint_w": 28, "footprint_h": 12, "icon": "fa-couch", "color": "#2563eb"
            },
            {
                "cat": "โต๊ะ", "zone_name": "โซนโต๊ะกลาง", "title": "กึ่งกลางหน้าโซฟา",
                "x": 48, "y": min(92, baseline_y + 16), "tip": "วางโต๊ะกลางด้านหน้าโซฟาเพื่อการใช้งานที่สะดวก",
                "footprint_w": 20, "footprint_h": 9, "icon": "fa-table", "color": "#f59e0b"
            },
            {
                "cat": "เก้าอี้", "zone_name": "มุมสนทนา", "title": "ด้านข้างโซฟา",
                "x": 76, "y": min(86, baseline_y + 8), "tip": "วางทำมุม 45 องศากับโซฟาเพื่อสร้างพื้นที่สนทนาที่อบอุ่น",
                "footprint_w": 16, "footprint_h": 8, "icon": "fa-chair", "color": "#10b981"
            },
            {
                "cat": "ตู้", "zone_name": "โซนจัดเก็บ", "title": "ชิดผนังด้านข้าง",
                "x": 20, "y": min(82, baseline_y + 4), "tip": "วางชิดผนังด้านข้างเพื่อจัดเก็บสิ่งของให้เรียบร้อย",
                "footprint_w": 22, "footprint_h": 9, "icon": "fa-box-archive", "color": "#8b5cf6"
            },
            {
                "cat": "ของตกแต่ง", "zone_name": "มุมตกแต่ง", "title": "มุมห้อง",
                "x": 84, "y": min(85, baseline_y + 6), "tip": "วางของตกแต่งหรือต้นไม้เพิ่มความสดชื่น",
                "footprint_w": 14, "footprint_h": 7, "icon": "fa-plant-wilt", "color": "#06b6d4"
            }
        ]

    bedroom_presets = [
        {
            "cat": "เตียง", "zone_name": "โซนเตียงนอนหลัก (Master Sleep)", "title": "กึ่งกลางผนังห้อง",
            "x": 50, "y": min(88, baseline_y + 5), "tip": "วางหัวเตียงชิดผนังหลักตรงกลางห้อง เว้นทางเดินรอบเตียงทั้งสองฝั่งเพื่อการสัญจรที่สะดวกสบาย",
            "footprint_w": 32, "footprint_h": 14, "icon": "fa-bed", "color": "#2563eb"
        },
        {
            "cat": "ตู้", "zone_name": "โซนตู้ข้างเตียง/ตู้เสื้อผ้า", "title": "ชิดผนังด้านข้างเตียง",
            "x": 80, "y": min(82, baseline_y + 4), "tip": "วางตู้ข้างเตียงในตำแหน่งที่เอื้อมหยิบโคมไฟหรือของใช้ส่วนตัวได้สะดวกขณะนอน",
            "footprint_w": 18, "footprint_h": 8, "icon": "fa-box-archive", "color": "#8b5cf6"
        },
        {
            "cat": "โต๊ะ", "zone_name": "มุมโต๊ะทำงาน/แต่งตัว", "title": "ริมหน้าต่างหรือมุมห้อง",
            "x": 22, "y": min(86, baseline_y + 8), "tip": "วางโต๊ะใกล้ช่องแสงธรรมชาติเพื่อให้ได้รับแสงธรรมชาติขณะทำงานหรือแต่งตัว",
            "footprint_w": 20, "footprint_h": 9, "icon": "fa-table", "color": "#f59e0b"
        },
        {
            "cat": "ของตกแต่ง", "zone_name": "มุมตกแต่งผ่อนคลาย", "title": "บนโต๊ะข้างเตียง / มุมห้อง",
            "x": 76, "y": min(84, baseline_y + 6), "tip": "จัดวางโคมไฟหรือของตกแต่งเพื่อสร้างบรรยากาศอบอุ่น ผ่อนคลายก่อนนอน",
            "footprint_w": 14, "footprint_h": 7, "icon": "fa-plant-wilt", "color": "#06b6d4"
        }
    ]

    working_presets = [
        {
            "cat": "โต๊ะ", "zone_name": "โซนโต๊ะทำงานหลัก (Focus Work)", "title": "ริมหน้าต่าง / กลางห้องทำงาน",
            "x": 50, "y": min(90, baseline_y + 12), "tip": "จัดวางโต๊ะทำงานในจุดที่รับแสงธรรมชาติได้ดี ไม่อยู่ในแนวแสงสะท้อนหน้าจอคอมพิวเตอร์",
            "footprint_w": 26, "footprint_h": 11, "icon": "fa-table", "color": "#f59e0b"
        },
        {
            "cat": "เก้าอี้", "zone_name": "เก้าอี้ทำงานสรีรศาสตร์ (Ergonomic)", "title": "ด้านหลังโต๊ะทำงาน",
            "x": 50, "y": min(85, baseline_y + 4), "tip": "จัดวางคู่กับโต๊ะทำงาน เว้นระยะถอยเก้าอี้และหมุนตัวได้อย่างสะดวกสบาย",
            "footprint_w": 16, "footprint_h": 8, "icon": "fa-chair", "color": "#2563eb"
        },
        {
            "cat": "ตู้", "zone_name": "โซนเอกสารและหนังสือ (Reference Wall)", "title": "ชิดผนังด้านข้าง",
            "x": 80, "y": min(82, baseline_y + 4), "tip": "วางตู้เก็บเอกสารหรือชั้นหนังสือชิดผนังด้านข้างเพื่อความเรียบร้อยและหยิบใช้งานง่าย",
            "footprint_w": 20, "footprint_h": 9, "icon": "fa-box-archive", "color": "#8b5cf6"
        },
        {
            "cat": "ของตกแต่ง", "zone_name": "มุมฟอกอากาศ (Air Purifying)", "title": "มุมห้อง / ริมโต๊ะ",
            "x": 20, "y": min(86, baseline_y + 8), "tip": "วางต้นไม้ฟอกอากาศขนาดเล็กช่วยเพิ่มความสดชื่นและลดความเหนื่อยล้าขณะทำงาน",
            "footprint_w": 14, "footprint_h": 7, "icon": "fa-plant-wilt", "color": "#06b6d4"
        }
    ]

    r_text = (room_type or "").lower()
    if any(k in r_text for k in ["นอน", "bedroom"]):
        presets = bedroom_presets
    elif any(k in r_text for k in ["ทำงาน", "working", "office"]):
        presets = working_presets
    else:
        presets = living_presets

    ai_map = {}
    if isinstance(ai_placements, list):
        for item in ai_placements:
            if isinstance(item, dict) and "id" in item:
                ai_map[item["id"]] = item

    placements = []
    used_cats = set()
    fallback_coords = [
        (36, min(88, baseline_y + 7)),
        (54, min(92, baseline_y + 16)),
        (78, min(86, baseline_y + 6)),
        (18, min(82, baseline_y + 4)),
        (86, min(85, baseline_y + 8))
    ]

    for idx, p in enumerate(products):
        pid = p['id']
        p_cat = p.get('category') or ''
        ai_item = ai_map.get(pid)

        preset = next((pr for pr in presets if pr['cat'] in p_cat and pr['cat'] not in used_cats), None)
        if preset:
            used_cats.add(preset['cat'])
            def_x, def_y = preset['x'], preset['y']
            def_title = preset['title']
            def_tip = preset['tip']
            def_zone = preset['zone_name']
            def_fw = preset.get('footprint_w', 22)
            def_fh = preset.get('footprint_h', 10)
            def_icon = preset.get('icon', 'fa-couch')
            def_color = preset.get('color', '#2563eb')
        else:
            def_x, def_y = fallback_coords[min(idx, len(fallback_coords) - 1)]
            def_title = f"จุดจัดวางที่ {idx + 1}"
            def_tip = f"จัดวาง {p.get('name', 'เฟอร์นิเจอร์')} ในจุดที่เสริมฟังก์ชันและความคล่องตัวของห้อง"
            def_zone = f"โซนจัดวางที่ {idx + 1}"
            def_fw = 20
            def_fh = 9
            def_icon = "fa-couch"
            def_color = "#2563eb"

        pos_title = def_title
        pos_tip = def_tip
        pos_zone = def_zone
        pos_x = def_x
        pos_y = def_y

        if ai_item:
            if isinstance(ai_item.get("position_title"), str) and ai_item["position_title"].strip():
                pos_title = ai_item["position_title"].strip()
            if isinstance(ai_item.get("zone_name"), str) and ai_item["zone_name"].strip():
                pos_zone = ai_item["zone_name"].strip()
            if isinstance(ai_item.get("tip"), str) and ai_item["tip"].strip():
                pos_tip = ai_item["tip"].strip()
            if isinstance(ai_item.get("x"), (int, float)) and 5 <= ai_item["x"] <= 95:
                pos_x = int(ai_item["x"])
            if isinstance(ai_item.get("y"), (int, float)) and 10 <= ai_item["y"] <= 95:
                pos_y = int(ai_item["y"])

        placements.append({
            "product_id": pid,
            "product_name": p.get("name", ""),
            "category": p_cat,
            "price": float(p.get("price", 0)),
            "pin_number": idx + 1,
            "x": pos_x,
            "y": pos_y,
            "zone_name": pos_zone,
            "position_title": pos_title,
            "placement_tip": pos_tip,
            "footprint_w": def_fw,
            "footprint_h": def_fh,
            "icon": def_icon,
            "color": def_color
        })

    return placements


# ==========================================
# API สำหรับ Ollama + PostgreSQL Strict Budget Recommendation
# ==========================================
@app.post("/recommend-products")
@app.post("/api/recommend-products")
def recommend_products(
    payload: RecommendRequest,
    x_user_id: Optional[str] = Header(None, alias="x-user-id"),
    user_id: Optional[str] = Header(None, alias="user-id"),
    conn=Depends(get_db)
):
    with conn.cursor() as cursor:
        # กรองสินค้าที่ราคาเกินงบต่อชิ้นออกไปก่อนเลย
        cursor.execute(
            "SELECT id, name, category, price, description FROM products WHERE is_active = TRUE AND price <= %s",
            (payload.budget,)
        )
        products = cursor.fetchall()

    if not products:
        raise HTTPException(
            status_code=404,
            detail=f"ไม่พบสินค้าที่เปิดใช้งาน (Active) และอยู่ในงบประมาณ {payload.budget:,.0f} บาท กรุณาตรวจสอบว่ามีสินค้าเปิดใช้งานในคลัง หรือลองปรับเพิ่มงบประมาณ"
        )

    # กรองเฉพาะหมวดที่เหมาะกับประเภทห้อง (เช่น ห้องรับแขกไม่ควรได้เตียง)
    products, room_filtered = filter_products_by_room(products, payload.room_type)
    allowed_cats = allowed_categories_for_room(payload.room_type) if room_filtered else None

    if not products:
        cats_str = f" (ต้องการหมวด: {', '.join(allowed_cats)})" if allowed_cats else ""
        raise HTTPException(
            status_code=404,
            detail=f"ไม่พบสินค้าที่เปิดใช้งานและเหมาะกับ{payload.room_type}{cats_str} กรุณาเปิดใช้งานสินค้าหมวดที่เกี่ยวข้องในคลังสินค้า (Orders) ก่อนครับ"
        )

    category_rule = (
        f"4. กฎเข้มงวด: เลือกได้เฉพาะสินค้าในหมวดหมู่ที่เหมาะกับ {payload.room_type} เท่านั้น ({', '.join(allowed_cats)}) "
        f"ห้ามเลือกสินค้าผิดประเภทห้องโดยเด็ดขาด (เช่น ห้ามเลือกเตียงสำหรับห้องรับแขก หรือห้องทำงาน) "
        f"และพยายามเลือกคนละหมวดกัน ไม่เลือกหมวดเดียวกันซ้ำ"
        if allowed_cats else ""
    )

    # วิเคราะห์องค์ประกอบของรูปภาพห้องจริง (AI Vision Spatial Analysis & Existing Furniture Detection)
    visual_info = analyze_room_image(payload.room_image) if payload.room_image else {
        'floor_baseline_y': 66,
        'window_side': 'right',
        'window_side_th': 'ขวา',
        'anchor_wall_th': 'ซ้ายและกึ่งกลาง',
        'light_analysis': 'ตรวจพบช่องรับแสงธรรมชาติที่เปิดความโปร่งโล่งให้ห้อง',
        'wall_analysis': 'ผนังทึบหลักเหมาะสำหรับเป็นจุดยึดสายตา (Anchor Point) ในการวางเฟอร์นิเจอร์ชิ้นหลัก',
        'floor_analysis': 'ระนาบพื้นห้องกว้างขวาง เหมาะสำหรับการจัดวางแบบเปิดทางสัญจร (Open Flow Circulation)',
        'existing_furniture': [],
        'has_existing_furniture': False,
        'existing_summary': 'ตรวจพบว่าเป็นห้องเปล่า ยังไม่มีเฟอร์นิเจอร์หลัก'
    }

    # หาก AI Vision ตรวจพบว่ามีเฟอร์นิเจอร์หลักชิ้นใดอยู่แล้วในห้อง (เช่น มีเตียงอยู่แล้ว หรือมีโซฟาอยู่แล้ว)
    # ให้ตัดหมวดนั้นออกจากรายการแนะนำทันที เพื่อป้องกันการแนะนำสินค้าซ้ำซ้อนกับของเดิมที่มีอยู่
    existing_items = visual_info.get('existing_furniture', [])
    if existing_items:
        non_duplicate_products = [
            p for p in products if not any(ex in (p['category'] or '') for ex in existing_items)
        ]
        if non_duplicate_products:
            products = non_duplicate_products

    product_catalog = []
    for p in products:
        product_catalog.append({
            "id": p['id'],
            "name": p['name'],
            "price": float(p['price']),
            "category": p['category']
        })

    existing_rule_text = (
        f"- ตรวจพบเฟอร์นิเจอร์เดิมในห้องจริง: {', '.join(existing_items)}\n"
        f"- กฎเหล็กป้องกันการซ้ำซ้อน: ภาพห้องนี้มี {', '.join(existing_items)} อยู่แล้ว **ห้ามเลือกสินค้าหมวดหมู่ {', '.join(existing_items)} ซ้ำโดยเด็ดขาด** "
        f"ให้เลือกเฉพาะเฟอร์นิเจอร์เสริม (เช่น ตู้ข้างเตียง, โต๊ะทำงาน, ตู้เสื้อผ้า, ของตกแต่ง) ที่นำมาจัดวางร่วมกับเฟอร์นิเจอร์เดิมได้ลงตัว\n"
        if existing_items else "- ตรวจพบว่าเป็นห้องเปล่า: สามารถแนะนำเฟอร์นิเจอร์หลักของห้องได้อย่างเต็มที่\n"
    )

    visual_context = (
        f"\nข้อมูลวิเคราะห์มิติห้องจากรูปภาพจริง (AI Vision Spatial Analysis):\n"
        f"- แนวดิ่งพื้นห้องจริง (Floor Baseline): เริ่มที่ระดับ {visual_info['floor_baseline_y']}% จากขอบบนภาพ (ห้ามวางของลอยบนผนังหรือเพดาน)\n"
        f"- ทิศทางแสงธรรมชาติ/หน้าต่าง: อยู่ทางฝั่ง{visual_info['window_side_th']}ของห้อง ({visual_info['light_analysis']})\n"
        f"- ผนังทึบหลักที่เหมาะเป็นจุดยึดสายตา (Anchor Wall): อยู่ทางฝั่ง{visual_info['anchor_wall_th']} ({visual_info['wall_analysis']})\n"
        f"{existing_rule_text}"
    )

    prompt_text = f"""
คุณคือ AI ผู้เชี่ยวชาญด้านการออกแบบตกแต่งภายในและจัดสรรพื้นที่ห้อง (Interior Spatial Layout Specialist)
นี่คือรายการสินค้าที่เหมาะกับห้องนี้และอยู่ในงบ:
{json.dumps(product_catalog, ensure_ascii=False)}
{visual_context}
โจทย์:
1. เลือกสินค้า 1-3 ชิ้นสำหรับ "{payload.room_type}" ในสไตล์ "{payload.style}"
2. ต้องเลือกสินค้าโดยที่เมื่อนำราคามาบวกกันแล้ว **ห้ามเกิน {payload.budget} บาท โดยเด็ดขาด**
3. selected_ids ต้องเป็นตัวเลขรหัส (ค่าในช่อง "id") ของสินค้าที่เลือกเท่านั้น ห้ามใส่ราคาหรือชื่อสินค้า
4. เขียนคำแนะนำภาษาไทยแบบเป็นกันเอง ห้ามใส่ตัวเลขราคา ราคารวม หรืองบประมาณในข้อความทุกช่อง (ระบบจะแสดงราคาจริงให้เอง)
5. ระบุจุดจัดวางในห้อง (placements) ของสินค้าแต่ละชิ้นที่เลือก โดยระบุชื่อตำแหน่ง (position_title เช่น "ผนังหลักฝั่งซ้าย", "กึ่งกลางหน้าโซฟา"), พิกัดตำแหน่งบนภาพ x (10-90) และ y (ตามแนวพื้นห้องจริง ระหว่าง {visual_info['floor_baseline_y'] + 4} ถึง 90) ในหน่วยเปอร์เซ็นต์, และคำแนะนำว่าทำไมถึงควรวางจุดนั้น (tip)
{category_rule}

ตอบกลับในรูปแบบ JSON สตริงแท้ๆ ในฟอร์แมตนี้เท่านั้น ห้ามมี markdown codeblock:
{{
  "selected_ids": [รหัส id ของสินค้าชิ้นที่เลือก, ...],
  "overview": "ภาพรวมการจัดห้อง 1-2 ประโยค",
  "reasons": [{{"id": รหัส id ของสินค้าชิ้นนั้น, "reason": "เหตุผลสั้นๆ ที่เลือกชิ้นนี้"}}],
  "placements": [
    {{
      "id": รหัส id ของสินค้าชิ้นนั้น,
      "position_title": "ชื่อตำแหน่งจัดวาง เช่น ผนังหลักฝั่งซ้าย / กลางห้อง",
      "x": 35,
      "y": 68,
      "tip": "คำแนะนำสั้นๆ ว่าทำไมควรวางชิ้นนี้ตรงจุดนี้"
    }}
  ]
}}
"""

    parsed_result = {}
    used_fallback = False
    raw_text = ""
    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": "llama3.2",
                "prompt": prompt_text,
                "stream": False,
                "format": "json",
                "keep_alive": "30m",
                "options": {
                    "num_gpu": 99,
                    "temperature": 0.3
                }
            },
            timeout=30
        )

        if response.status_code == 200:
            res_data = response.json()
            raw_text = res_data.get("response", "{}")
            try:
                parsed_result = json.loads(raw_text)
            except (TypeError, ValueError):
                parsed_result = {}
            if not isinstance(parsed_result, dict):
                parsed_result = {}
        else:
            print(f"[recommend] Ollama returned status {response.status_code}, switching to fallback.")
            used_fallback = True
    except Exception as ollama_err:
        print(f"[recommend] Ollama connection failed: {ollama_err}, switching to fallback.")
        used_fallback = True

    try:
        # รับ id ได้หลายรูปแบบ และรับเฉพาะ id ที่อยู่ในรายการสินค้าที่ส่งให้ AI จริง
        selected_ids = resolve_selected_ids(parsed_result.get("selected_ids"), products) if not used_fallback else []
        if not selected_ids:
            # AI ตอบ id ใช้ไม่ได้ หรือ Ollama ไม่พร้อม -> ให้ระบบคัดสินค้าตามหมวดที่เหมาะกับห้องและงบแทน
            used_fallback = True
            selected_ids = fallback_pick_products(products, payload.room_type, payload.budget)
            print(f"[recommend] AI ตอบ selected_ids ใช้ไม่ได้ หรือใช้ Fallback. raw_response={raw_text[:500]!r}")
        print(f"[recommend] room={payload.room_type!r} budget={payload.budget} "
              f"ai_ids={parsed_result.get('selected_ids')!r} used={selected_ids} fallback={used_fallback}")

        matched_products = []
        if selected_ids:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, name, category, price, description, image_data, image_url, style_tags, purchase_url AS product_url FROM products WHERE id = ANY(%s)",
                    (selected_ids,)
                )
                rows = cursor.fetchall()
                for r in rows:
                    prod = dict(r)
                    if prod.get('image_data'):
                        img_bytes = prod['image_data']
                        if isinstance(img_bytes, memoryview):
                            img_bytes = img_bytes.tobytes()
                        encoded = base64.b64encode(img_bytes).decode('utf-8')
                        prod['image_base64'] = f"data:image/jpeg;base64,{encoded}"
                    else:
                        prod['image_base64'] = None
                    prod.pop('image_data', None)
                    prod['price'] = float(prod['price'])
                    matched_products.append(prod)

        # Backend Validation: คำนวณราคาจริงและตัดของออกถ้า AI ส่งเกินงบ
        matched_products.sort(key=lambda x: x['price'], reverse=True)  # เรียงจากแพงไปถูก
        real_total_price = sum(p['price'] for p in matched_products)

        # ตัดสินค้าชิ้นที่แพงที่สุดออกทีละชิ้น จนกว่าราคารวมจะอยู่ในงบ
        trimmed = False
        while real_total_price > payload.budget and matched_products:
            real_total_price -= matched_products.pop(0)['price']
            trimmed = True

        real_total_price = sum(p['price'] for p in matched_products)

        # ประกอบข้อความจากสินค้าจริง ชื่อและราคาตรงกับการ์ดที่แสดง ไม่ใช้ตัวเลขที่ AI เขียนเอง
        recommendation_text = compose_recommendation(
            overview="" if used_fallback else (parsed_result.get("overview") or parsed_result.get("recommendation") or ""),
            products=matched_products,
            reasons={} if used_fallback else parse_reasons(parsed_result, products),
            room_type=payload.room_type,
            budget=payload.budget,
            trimmed=trimmed,
            used_fallback=used_fallback,
        )

        # คำนวณพิกัดจัดวางเฟอร์นิเจอร์บนภาพห้อง (Spatial Layout Placements) โดยอิงตามการวิเคราะห์มิติภาพจริง
        placements = compute_spatial_placements(
            products=matched_products,
            room_type=payload.room_type,
            visual_info=visual_info,
            ai_placements=parsed_result.get("placements") if isinstance(parsed_result, dict) else None
        )

        # สรุปแนวคิดการออกแบบเชิงสถาปัตยกรรมภายใน (Interior Design Concept)
        design_concept = {
            "style_name": payload.style,
            "room_type": payload.room_type,
            "headline": f"แนวคิดการจัดผังแบบ Open Flow ผสานแสงธรรมชาติ ({payload.style})",
            "story": (
                f"การออกแบบพื้นที่สำหรับ {payload.room_type} สไตล์ {payload.style} มุ่งเน้นความโปร่งโล่งสบายตา "
                f"โดยใช้{visual_info['anchor_wall_th']}เป็นจุดยึดสายตาหลัก (Anchor Point) ในการจัดวางเฟอร์นิเจอร์หลัก "
                f"และเปิดพื้นที่{visual_info['window_side_th']}เพื่อรับแสงธรรมชาติอย่างเต็มที่ พร้อมเว้นทางสัญจรที่ลื่นไหลไม่อึดอัด"
            ),
            "visual_insights": [
                {"icon": "fa-sun", "label": "แสงธรรมชาติ", "desc": visual_info['light_analysis']},
                {"icon": "fa-border-all", "label": "จุดยึดสายตา", "desc": visual_info['wall_analysis']},
                {"icon": "fa-shoe-prints", "label": "การสัญจรและพื้นห้อง", "desc": visual_info['floor_analysis']}
            ]
        }

        # บันทึกว่า AI เลือกสินค้าเหล่านี้ให้ลูกค้า (เฉพาะชิ้นที่ผ่านการตรวจงบแล้ว)
        uid = parse_optional_user_id(x_user_id, user_id)
        log_product_events(conn, [p['id'] for p in matched_products], 'ai_pick', uid)

        # บันทึกประวัติการออกแบบลงฐานข้อมูล (Cloud Sync ข้ามอุปกรณ์ PC/มือถือ)
        current_uname = None
        if uid:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT username FROM users WHERE id = %s", (uid,))
                    urow = cur.fetchone()
                    if urow:
                        current_uname = urow['username']
            except Exception:
                pass

        saved_design_id = None
        try:
            design_payload = {
                "recommendation": recommendation_text,
                "matched_products": matched_products,
                "placements": placements,
                "design_concept": design_concept,
                "visual_info": visual_info
            }
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO user_room_designs (user_id, username, room_type, style, budget, image_url, design_data)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        uid,
                        current_uname,
                        payload.room_type,
                        payload.style,
                        payload.budget,
                        payload.room_image or "",
                        json.dumps(design_payload, ensure_ascii=False)
                    )
                )
                saved_design_id = cur.fetchone()['id']
            conn.commit()
        except Exception as save_err:
            print(f"[Warning] Could not save user_room_designs: {save_err}")

        return {
            "status": "success",
            "id": saved_design_id,
            "budget": payload.budget,
            "total_price": real_total_price,
            "recommendation": recommendation_text,
            "matched_products": matched_products,
            "placements": placements,
            "design_concept": design_concept,
            "visual_info": visual_info
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"เกิดข้อผิดพลาดในการคำนวณของ AI: {str(e)}")


# ==========================================
# API สำหรับดึงผลลัพธ์การออกแบบล่าสุด (Cloud Sync ระหว่างคอมและมือถือ)
# ==========================================
@app.get("/api/latest-room-design")
def get_latest_room_design(
    x_user_id: Optional[str] = Header(None, alias="x-user-id"),
    user_id: Optional[str] = Header(None, alias="user-id"),
    uid: Optional[int] = None,
    conn=Depends(get_db)
):
    target_uid = uid or parse_optional_user_id(x_user_id, user_id)
    with conn.cursor() as cur:
        row = None
        if target_uid:
            cur.execute(
                """
                SELECT id, user_id, username, room_type, style, budget, image_url, design_data, created_at
                FROM user_room_designs
                WHERE user_id = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (target_uid,)
            )
            row = cur.fetchone()

        if not row:
            cur.execute(
                """
                SELECT id, user_id, username, room_type, style, budget, image_url, design_data, created_at
                FROM user_room_designs
                ORDER BY id DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="ยังไม่มีข้อมูลการออกแบบในระบบ")

        raw_data = row['design_data']
        design_data = raw_data if isinstance(raw_data, dict) else json.loads(raw_data or '{}')
        return {
            "status": "success",
            "id": row['id'],
            "userId": row['user_id'],
            "username": row['username'],
            "roomType": row['room_type'],
            "style": row['style'],
            "budget": float(row['budget'] or 0),
            "image": row['image_url'],
            "aiRecommendation": design_data.get('recommendation', ''),
            "matchedProducts": design_data.get('matched_products', []),
            "placements": design_data.get('placements', []),
            "designConcept": design_data.get('design_concept'),
            "visualInfo": design_data.get('visual_info'),
            "createdAt": row['created_at'].isoformat() if row.get('created_at') else None
        }


# ==========================================
# API สำหรับบันทึกการคลิกสั่งซื้อ
# ==========================================
@app.post("/api/products/{product_id}/click")
def record_store_click(
    product_id: int,
    x_user_id: Optional[str] = Header(None, alias="x-user-id"),
    user_id: Optional[str] = Header(None, alias="user-id"),
    conn=Depends(get_db)
):
    with conn.cursor() as cursor:
        cursor.execute("SELECT 1 FROM products WHERE id = %s AND is_active = TRUE", (product_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="ไม่พบสินค้า")

    log_product_events(conn, [product_id], 'store_click', parse_optional_user_id(x_user_id, user_id))
    return {"message": "บันทึกการคลิกแล้ว"}


# ==========================================
# API สำหรับดึงสถิติร้านค้า
# ==========================================
@app.get("/api/seller-stats")
def get_seller_stats(
    x_user_id: Optional[str] = Header(None, alias="x-user-id"),
    user_id: Optional[str] = Header(None, alias="user-id"),
    conn=Depends(get_db)
):
    uid = get_valid_user_id(x_user_id, user_id)

    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT username, role FROM users WHERE id = %s", (uid,))
            user = cursor.fetchone()
            
            if not user or user['role'] != 'seller':
                return {"total_products": 0, "ai_usage": 0, "store_clicks": 0}

            cursor.execute("SELECT COUNT(*) as total FROM products WHERE seller_name = %s", (user['username'],))
            total_products = cursor.fetchone()['total']

            event_counts = count_events(conn, user['username'])

            return {
                "total_products": total_products,
                "ai_usage": event_counts["ai_pick"],
                "store_clicks": event_counts["store_click"]
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))