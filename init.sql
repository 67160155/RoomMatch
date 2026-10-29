-- เปิดใช้งาน Vector (ต้องติดตั้ง pgvector ใน PostgreSQL server ก่อน)
CREATE EXTENSION IF NOT EXISTS vector;

-- สร้างตาราง Users
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(20) DEFAULT 'customer', -- เพิ่มคอลัมน์ role สำหรับกำหนดสิทธิ์
    is_active BOOLEAN DEFAULT TRUE,      -- นำมารวมใน CREATE TABLE ได้เลย
    avatar_url TEXT,                     -- รูปโปรไฟล์จาก Google หรือ Avatar
    google_id VARCHAR(100),              -- Google Account Sub/ID
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id VARCHAR(100);

-- สร้างตาราง Products สำหรับ AI Matching
CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    category VARCHAR(50) NOT NULL,
    price DECIMAL(10, 2) NOT NULL,
    description TEXT,
    image_data BYTEA,            -- เพิ่มสำหรับเก็บไฟล์ภาพตรงๆ ตามโค้ดใน API
    seller_name VARCHAR(50),     -- เพิ่มสำหรับระบุร้านค้าที่เป็นคนเพิ่มสินค้า
    image_url TEXT,              -- ลบ NOT NULL ออก เนื่องจากใน API มีการส่งค่าว่างเข้าไป
    purchase_url TEXT,           -- คอลัมน์สำหรับเก็บลิงก์สั่งซื้อ
    style_tags TEXT[],
    image_embedding vector(512), -- เก็บ Vector จากโมเดล CLIP (512 dimensions)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- สร้าง Index เพื่อเพิ่มความเร็วในการค้นหา Vector
CREATE INDEX IF NOT EXISTS idx_products_image_embedding 
ON products USING hnsw (image_embedding vector_cosine_ops);

ALTER TABLE products ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;