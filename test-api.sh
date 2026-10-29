#!/bin/bash

BASE_URL="http://localhost/api"

echo "=== 1. Testing Root API ==="
curl -s -X GET "$BASE_URL/"
echo -e "\n"

echo "=== 2. Testing Register ==="
curl -s -X POST "$BASE_URL/register" \
  -H "Content-Type: application/json" \
  -d '{"username": "testuser", "email": "test@example.com", "password": "password123"}'
echo -e "\n"

echo "=== 3. Testing Login ==="
curl -s -X POST "$BASE_URL/login" \
  -H "Content-Type: application/json" \
  -d '{"username": "testuser", "password": "password123"}'
echo -e "\n"

echo "=== 4. Testing Get User Profile (ID: 1) ==="
curl -s -X GET "$BASE_URL/users/1"
echo -e "\n"