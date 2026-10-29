FROM nginx:alpine

# ลบ config เริ่มต้นของ Nginx
RUN rm /etc/nginx/conf.d/default.conf

# ใส่ config ของเราเอง
COPY nginx.conf /etc/nginx/conf.d/

# ก๊อปปี้ไฟล์หน้าเว็บทั้งหมดไปไว้ใน Nginx
COPY html /usr/share/nginx/html

EXPOSE 80