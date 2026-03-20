#!/usr/bin/env bash
# =============================================================================
# Integration tests for BeautyGO dev server
#
# Usage:
#   ./tests/test_api_dev.sh                        # default: http://localhost:8000
#   ./tests/test_api_dev.sh https://dev.beautygo.ru # custom server
#   BASE_URL=https://dev.beautygo.ru ./tests/test_api_dev.sh
#
# Requirements: curl, jq
# =============================================================================

set -euo pipefail

BASE_URL="${1:-${BASE_URL:-http://localhost:8000}}"
API="${BASE_URL}/api/v1"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

PASSED=0
FAILED=0
TOTAL=0
FAILURES=""

# --- Helpers ---

assert() {
    local name="$1"
    local expected="$2"
    local actual="$3"
    TOTAL=$((TOTAL + 1))

    if [ "$expected" = "$actual" ]; then
        echo -e "  ${GREEN}✓${NC} ${name}"
        PASSED=$((PASSED + 1))
    else
        echo -e "  ${RED}✗${NC} ${name}"
        echo -e "    expected: ${YELLOW}${expected}${NC}"
        echo -e "    actual:   ${RED}${actual}${NC}"
        FAILED=$((FAILED + 1))
        FAILURES="${FAILURES}\n  ✗ ${name} (expected=${expected}, got=${actual})"
    fi
}

assert_not() {
    local name="$1"
    local unexpected="$2"
    local actual="$3"
    TOTAL=$((TOTAL + 1))

    if [ "$unexpected" != "$actual" ]; then
        echo -e "  ${GREEN}✓${NC} ${name}"
        PASSED=$((PASSED + 1))
    else
        echo -e "  ${RED}✗${NC} ${name}"
        echo -e "    should NOT be: ${RED}${unexpected}${NC}"
        FAILED=$((FAILED + 1))
        FAILURES="${FAILURES}\n  ✗ ${name} (should not be ${unexpected})"
    fi
}

assert_contains() {
    local name="$1"
    local substring="$2"
    local body="$3"
    TOTAL=$((TOTAL + 1))

    if echo "$body" | grep -q "$substring"; then
        echo -e "  ${GREEN}✓${NC} ${name}"
        PASSED=$((PASSED + 1))
    else
        echo -e "  ${RED}✗${NC} ${name}"
        echo -e "    body does not contain: ${YELLOW}${substring}${NC}"
        echo -e "    body: ${RED}${body:0:200}${NC}"
        FAILED=$((FAILED + 1))
        FAILURES="${FAILURES}\n  ✗ ${name} (missing '${substring}')"
    fi
}

assert_json_field() {
    local name="$1"
    local jq_expr="$2"
    local expected="$3"
    local body="$4"
    local actual
    actual=$(echo "$body" | jq -r "$jq_expr" 2>/dev/null || echo "JQ_ERROR")
    assert "$name" "$expected" "$actual"
}

# Make HTTP request, capture status + body
# Usage: http METHOD path [data] [extra_curl_args...]
# Sets: HTTP_STATUS, HTTP_BODY
http() {
    local method="$1"
    local path="$2"
    local data="${3:-}"
    shift 2; [ -n "$data" ] && shift || true

    local curl_args=(-s -w '\n%{http_code}' -X "$method")
    curl_args+=(-H "Content-Type: application/json")

    # Pass remaining args (headers etc.)
    curl_args+=("$@")

    if [ -n "$data" ]; then
        curl_args+=(-d "$data")
    fi

    local response
    response=$(curl "${curl_args[@]}" "${API}${path}" 2>/dev/null) || {
        HTTP_STATUS="000"
        HTTP_BODY='{"error": "connection refused"}'
        return
    }

    HTTP_STATUS=$(echo "$response" | tail -1)
    HTTP_BODY=$(echo "$response" | sed '$d')
}

# Generate unique phone for test isolation
RANDOM_SUFFIX=$((RANDOM % 9000 + 1000))
TEST_PHONE="+7900${RANDOM_SUFFIX}001"
TEST_PHONE2="+7900${RANDOM_SUFFIX}002"
TEST_PHONE3="+7900${RANDOM_SUFFIX}003"
TEST_PHONE_SPECIALIST="+7900${RANDOM_SUFFIX}004"

echo ""
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  BeautyGO API Integration Tests${NC}"
echo -e "${CYAN}  Server: ${BASE_URL}${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
echo ""

# =============================================================================
# 0. CONNECTIVITY
# =============================================================================
echo -e "${CYAN}[0] Server connectivity${NC}"

http GET "/health/"
assert "Health endpoint returns 200" "200" "$HTTP_STATUS"
assert_json_field "Health body has status=ok" ".status" "ok" "$HTTP_BODY"


# =============================================================================
# 1. X-APP-TYPE MIDDLEWARE
# =============================================================================
echo ""
echo -e "${CYAN}[1] X-App-Type middleware${NC}"

# 1.1 Missing header
response=$(curl -s -w '\n%{http_code}' -X POST "${API}/auth/login/" \
    -H "Content-Type: application/json" \
    -d '{"phone": "+79001234567"}' 2>/dev/null)
status=$(echo "$response" | tail -1)
body=$(echo "$response" | sed '$d')
assert "Missing X-App-Type → 403" "403" "$status"
assert_json_field "Error code = APP_TYPE_MISSING" ".error.code" "APP_TYPE_MISSING" "$body"

# 1.2 Invalid header value
response=$(curl -s -w '\n%{http_code}' -X POST "${API}/auth/login/" \
    -H "Content-Type: application/json" \
    -H "X-App-Type: hacker" \
    -d '{"phone": "+79001234567"}' 2>/dev/null)
status=$(echo "$response" | tail -1)
body=$(echo "$response" | sed '$d')
assert "Invalid X-App-Type → 403" "403" "$status"
assert_json_field "Error code = APP_TYPE_INVALID" ".error.code" "APP_TYPE_INVALID" "$body"

# 1.3 Empty header
response=$(curl -s -w '\n%{http_code}' -X POST "${API}/auth/login/" \
    -H "Content-Type: application/json" \
    -H "X-App-Type: " \
    -d '{"phone": "+79001234567"}' 2>/dev/null)
status=$(echo "$response" | tail -1)
assert "Empty X-App-Type → 403" "403" "$status"

# 1.4 Health bypass
response=$(curl -s -w '\n%{http_code}' "${API}/health/" 2>/dev/null)
status=$(echo "$response" | tail -1)
assert "Health endpoint bypasses middleware (no header needed)" "200" "$status"

# 1.5 Docs bypass
response=$(curl -s -w '\n%{http_code}' "${BASE_URL}/api/docs/" 2>/dev/null)
status=$(echo "$response" | tail -1)
assert_not "Docs endpoint bypasses middleware" "403" "$status"

# 1.6 Case sensitivity
response=$(curl -s -w '\n%{http_code}' -X POST "${API}/auth/login/" \
    -H "Content-Type: application/json" \
    -H "X-App-Type: CLIENT" \
    -d '{"phone": "+79001234567"}' 2>/dev/null)
status=$(echo "$response" | tail -1)
assert "X-App-Type is case-sensitive (CLIENT ≠ client) → 403" "403" "$status"


# =============================================================================
# 2. REGISTRATION
# =============================================================================
echo ""
echo -e "${CYAN}[2] Registration (POST /auth/register/)${NC}"

# 2.1 Successful client registration
http POST "/auth/register/" "{\"phone\": \"${TEST_PHONE}\"}" -H "X-App-Type: client"
assert "Register client → 201" "201" "$HTTP_STATUS"
assert_json_field "Response has data.phone" ".data.phone" "$TEST_PHONE" "$HTTP_BODY"
assert_json_field "Response has OTP sent message" ".data.message" "OTP sent" "$HTTP_BODY"

# 2.2 Successful specialist registration (pro app)
http POST "/auth/register/" "{\"phone\": \"${TEST_PHONE_SPECIALIST}\"}" -H "X-App-Type: pro"
assert "Register specialist → 201" "201" "$HTTP_STATUS"
assert_json_field "Pro registration returns phone" ".data.phone" "$TEST_PHONE_SPECIALIST" "$HTTP_BODY"

# 2.3 Duplicate phone
# Wait for rate limit to avoid false positive
sleep 1
http POST "/auth/register/" "{\"phone\": \"${TEST_PHONE}\"}" -H "X-App-Type: client"
assert "Duplicate phone → 400" "400" "$HTTP_STATUS"
assert_json_field "Error code = PHONE_ALREADY_REGISTERED" ".error.code" "PHONE_ALREADY_REGISTERED" "$HTTP_BODY"

# 2.4 Missing phone field
http POST "/auth/register/" '{}' -H "X-App-Type: client"
assert "Missing phone → 400" "400" "$HTTP_STATUS"
assert_json_field "Error code = VALIDATION_ERROR" ".error.code" "VALIDATION_ERROR" "$HTTP_BODY"

# 2.5 Invalid phone format (too short)
http POST "/auth/register/" '{"phone": "+7900123"}' -H "X-App-Type: client"
assert "Short phone → 400" "400" "$HTTP_STATUS"

# 2.6 Invalid phone format (wrong country)
http POST "/auth/register/" '{"phone": "+19001234567"}' -H "X-App-Type: client"
assert "Non-Russian phone → 400" "400" "$HTTP_STATUS"

# 2.7 Empty body
http POST "/auth/register/" '' -H "X-App-Type: client"
assert "Empty body → 400" "400" "$HTTP_STATUS"

# 2.8 Invalid JSON
response=$(curl -s -w '\n%{http_code}' -X POST "${API}/auth/register/" \
    -H "Content-Type: application/json" \
    -H "X-App-Type: client" \
    -d 'not json at all' 2>/dev/null)
status=$(echo "$response" | tail -1)
assert "Malformed JSON → 400" "400" "$status"

# 2.9 Phone normalization (8 → +7)
http POST "/auth/register/" "{\"phone\": \"8900${RANDOM_SUFFIX}009\"}" -H "X-App-Type: client"
assert "Phone with 8-prefix → 201 (normalized)" "201" "$HTTP_STATUS"
assert_json_field "Normalized to +7" ".data.phone" "+7900${RANDOM_SUFFIX}009" "$HTTP_BODY"

# 2.10 Phone with spaces
http POST "/auth/register/" "{\"phone\": \"+7 900 ${RANDOM_SUFFIX} 008\"}" -H "X-App-Type: client"
# May be 201 or 400 depending on space handling in registration dedup
# Just check it doesn't 500
assert_not "Phone with spaces doesn't 500" "500" "$HTTP_STATUS"


# =============================================================================
# 3. LOGIN
# =============================================================================
echo ""
echo -e "${CYAN}[3] Login (POST /auth/login/)${NC}"

# Need to wait for OTP rate limit from registration
sleep 2

# 3.1 Login with registered phone
http POST "/auth/login/" "{\"phone\": \"${TEST_PHONE}\"}" -H "X-App-Type: client"
# May be 200 or 429 (rate limit from registration OTP)
if [ "$HTTP_STATUS" = "429" ]; then
    echo -e "  ${YELLOW}⚠${NC} Rate limited after registration, waiting..."
    sleep 60
    http POST "/auth/login/" "{\"phone\": \"${TEST_PHONE}\"}" -H "X-App-Type: client"
fi
assert "Login existing user → 200" "200" "$HTTP_STATUS"
assert_json_field "Login returns OTP sent" ".data.message" "OTP sent" "$HTTP_BODY"

# 3.2 Login with unregistered phone
http POST "/auth/login/" '{"phone": "+79099999999"}' -H "X-App-Type: client"
assert "Login unknown phone → 404" "404" "$HTTP_STATUS"
assert_json_field "Error code = USER_NOT_FOUND" ".error.code" "USER_NOT_FOUND" "$HTTP_BODY"

# 3.3 Login without phone
http POST "/auth/login/" '{}' -H "X-App-Type: client"
assert "Login empty body → 400" "400" "$HTTP_STATUS"

# 3.4 Login with invalid phone
http POST "/auth/login/" '{"phone": "abc"}' -H "X-App-Type: client"
assert "Login invalid phone → 400" "400" "$HTTP_STATUS"


# =============================================================================
# 4. VERIFY OTP
# =============================================================================
echo ""
echo -e "${CYAN}[4] Verify OTP (POST /auth/verify-otp/)${NC}"

# Register fresh user for OTP tests
http POST "/auth/register/" "{\"phone\": \"${TEST_PHONE2}\"}" -H "X-App-Type: client"

# 4.1 Correct OTP (debug code = 000000)
http POST "/auth/verify-otp/" "{\"phone\": \"${TEST_PHONE2}\", \"code\": \"000000\"}" -H "X-App-Type: client"
assert "Verify correct OTP → 200" "200" "$HTTP_STATUS"
# Check access token exists (non-null)
ACCESS_TOKEN=$(echo "$HTTP_BODY" | jq -r '.data.access' 2>/dev/null)
REFRESH_TOKEN=$(echo "$HTTP_BODY" | jq -r '.data.refresh' 2>/dev/null)
TOTAL=$((TOTAL + 1))
if [ -n "$ACCESS_TOKEN" ] && [ "$ACCESS_TOKEN" != "null" ]; then
    echo -e "  ${GREEN}✓${NC} Access token present"
    PASSED=$((PASSED + 1))
else
    echo -e "  ${RED}✗${NC} Access token missing"
    FAILED=$((FAILED + 1))
    FAILURES="${FAILURES}\n  ✗ Access token missing"
fi

TOTAL=$((TOTAL + 1))
if [ -n "$REFRESH_TOKEN" ] && [ "$REFRESH_TOKEN" != "null" ]; then
    echo -e "  ${GREEN}✓${NC} Refresh token present"
    PASSED=$((PASSED + 1))
else
    echo -e "  ${RED}✗${NC} Refresh token missing"
    FAILED=$((FAILED + 1))
    FAILURES="${FAILURES}\n  ✗ Refresh token missing"
fi

assert_json_field "User is verified" ".data.user.is_verified" "true" "$HTTP_BODY"
assert_json_field "User role is client" ".data.user.role" "client" "$HTTP_BODY"
assert_json_field "User phone matches" ".data.user.phone" "$TEST_PHONE2" "$HTTP_BODY"

# 4.2 Wrong OTP code
# Register another user
http POST "/auth/register/" "{\"phone\": \"${TEST_PHONE3}\"}" -H "X-App-Type: client"
http POST "/auth/verify-otp/" "{\"phone\": \"${TEST_PHONE3}\", \"code\": \"999999\"}" -H "X-App-Type: client"
assert "Wrong OTP → 400" "400" "$HTTP_STATUS"
assert_json_field "Error code = INVALID_OTP" ".error.code" "INVALID_OTP" "$HTTP_BODY"

# 4.3 Missing fields
http POST "/auth/verify-otp/" '{"phone": "+79001234567"}' -H "X-App-Type: client"
assert "Missing code field → 400" "400" "$HTTP_STATUS"

http POST "/auth/verify-otp/" '{"code": "000000"}' -H "X-App-Type: client"
assert "Missing phone field → 400" "400" "$HTTP_STATUS"

# 4.4 Non-numeric code
http POST "/auth/verify-otp/" '{"phone": "+79001234567", "code": "abcdef"}' -H "X-App-Type: client"
assert "Non-numeric code → 400" "400" "$HTTP_STATUS"

# 4.5 Max attempts exhaustion
# Try wrong code multiple times on TEST_PHONE3
http POST "/auth/verify-otp/" "{\"phone\": \"${TEST_PHONE3}\", \"code\": \"111111\"}" -H "X-App-Type: client"
http POST "/auth/verify-otp/" "{\"phone\": \"${TEST_PHONE3}\", \"code\": \"222222\"}" -H "X-App-Type: client"
# Third wrong attempt should trigger max attempts
assert "Max OTP attempts → 429" "429" "$HTTP_STATUS"
assert_json_field "Error code = MAX_ATTEMPTS_EXCEEDED" ".error.code" "MAX_ATTEMPTS_EXCEEDED" "$HTTP_BODY"


# =============================================================================
# 5. AUTHENTICATED ENDPOINTS
# =============================================================================
echo ""
echo -e "${CYAN}[5] Authenticated endpoints${NC}"

# 5.1 Profile without auth
http GET "/auth/profile/me/" "" -H "X-App-Type: client"
assert "Profile without auth → 401" "401" "$HTTP_STATUS"

# 5.2 Profile with valid token
if [ -n "$ACCESS_TOKEN" ] && [ "$ACCESS_TOKEN" != "null" ]; then
    http GET "/auth/profile/me/" "" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${ACCESS_TOKEN}"
    assert "Profile with auth → 200" "200" "$HTTP_STATUS"
    # ProfileDetailView uses DRF default (no "data" wrapper)
    assert_contains "Profile response is valid JSON" '"id"' "$HTTP_BODY"
fi

# 5.3 Invalid token
http GET "/auth/profile/me/" "" \
    -H "X-App-Type: client" \
    -H "Authorization: Bearer invalid.token.here"
assert "Invalid token → 401" "401" "$HTTP_STATUS"

# 5.4 Expired/garbage token
http GET "/auth/profile/me/" "" \
    -H "X-App-Type: client" \
    -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0b2tlbl90eXBlIjoiYWNjZXNzIiwiZXhwIjoxMDAwMDAwMDAwfQ.invalid"
assert "Forged token → 401" "401" "$HTTP_STATUS"

# 5.5 Missing Authorization header
http GET "/auth/profile/me/" "" -H "X-App-Type: client"
assert "No Authorization header → 401" "401" "$HTTP_STATUS"


# =============================================================================
# 6. LOGOUT
# =============================================================================
echo ""
echo -e "${CYAN}[6] Logout (POST /auth/logout/)${NC}"

if [ -n "$ACCESS_TOKEN" ] && [ "$ACCESS_TOKEN" != "null" ] && \
   [ -n "$REFRESH_TOKEN" ] && [ "$REFRESH_TOKEN" != "null" ]; then

    # 6.1 Logout with valid refresh token
    http POST "/auth/logout/" "{\"refresh\": \"${REFRESH_TOKEN}\"}" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${ACCESS_TOKEN}"
    assert "Logout → 200" "200" "$HTTP_STATUS"

    # 6.2 Reuse blacklisted refresh token
    http POST "/auth/logout/" "{\"refresh\": \"${REFRESH_TOKEN}\"}" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${ACCESS_TOKEN}"
    assert "Reuse blacklisted token → 400" "400" "$HTTP_STATUS"
fi

# 6.3 Logout without auth
http POST "/auth/logout/" '{"refresh": "some_token"}' -H "X-App-Type: client"
assert "Logout without auth → 401" "401" "$HTTP_STATUS"

# 6.4 Logout with missing refresh field
if [ -n "$ACCESS_TOKEN" ] && [ "$ACCESS_TOKEN" != "null" ]; then
    http POST "/auth/logout/" '{}' \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${ACCESS_TOKEN}"
    assert "Logout without refresh field → 400" "400" "$HTTP_STATUS"
fi


# =============================================================================
# 7. TOKEN REFRESH
# =============================================================================
echo ""
echo -e "${CYAN}[7] Token refresh (POST /auth/token/refresh/)${NC}"

# Get fresh tokens
http POST "/auth/register/" "{\"phone\": \"+7900${RANDOM_SUFFIX}050\"}" -H "X-App-Type: client"
http POST "/auth/verify-otp/" "{\"phone\": \"+7900${RANDOM_SUFFIX}050\", \"code\": \"000000\"}" -H "X-App-Type: client"
FRESH_REFRESH=$(echo "$HTTP_BODY" | jq -r '.data.refresh' 2>/dev/null)

if [ -n "$FRESH_REFRESH" ] && [ "$FRESH_REFRESH" != "null" ]; then
    # 7.1 Valid refresh
    http POST "/auth/token/refresh/" "{\"refresh\": \"${FRESH_REFRESH}\"}" -H "X-App-Type: client"
    assert "Token refresh → 200" "200" "$HTTP_STATUS"
    NEW_ACCESS=$(echo "$HTTP_BODY" | jq -r '.access' 2>/dev/null)
    TOTAL=$((TOTAL + 1))
    if [ -n "$NEW_ACCESS" ] && [ "$NEW_ACCESS" != "null" ]; then
        echo -e "  ${GREEN}✓${NC} New access token received"
        PASSED=$((PASSED + 1))
    else
        echo -e "  ${RED}✗${NC} No new access token"
        FAILED=$((FAILED + 1))
        FAILURES="${FAILURES}\n  ✗ No new access token from refresh"
    fi
fi

# 7.2 Invalid refresh token
http POST "/auth/token/refresh/" '{"refresh": "garbage"}' -H "X-App-Type: client"
assert "Invalid refresh token → 401" "401" "$HTTP_STATUS"

# 7.3 Empty body
http POST "/auth/token/refresh/" '{}' -H "X-App-Type: client"
assert "Refresh with empty body → 400" "400" "$HTTP_STATUS"


# =============================================================================
# 8. SERVICES CRUD (specialist only)
# =============================================================================
echo ""
echo -e "${CYAN}[8] Services CRUD (specialist only)${NC}"

# Get specialist tokens
sleep 1
http POST "/auth/login/" "{\"phone\": \"${TEST_PHONE_SPECIALIST}\"}" -H "X-App-Type: pro"
if [ "$HTTP_STATUS" = "429" ]; then
    sleep 61
    http POST "/auth/login/" "{\"phone\": \"${TEST_PHONE_SPECIALIST}\"}" -H "X-App-Type: pro"
fi
http POST "/auth/verify-otp/" "{\"phone\": \"${TEST_PHONE_SPECIALIST}\", \"code\": \"000000\"}" -H "X-App-Type: pro"
SPEC_ACCESS=$(echo "$HTTP_BODY" | jq -r '.data.access' 2>/dev/null)

# Get client tokens for comparison
http POST "/auth/register/" "{\"phone\": \"+7900${RANDOM_SUFFIX}060\"}" -H "X-App-Type: client"
http POST "/auth/verify-otp/" "{\"phone\": \"+7900${RANDOM_SUFFIX}060\", \"code\": \"000000\"}" -H "X-App-Type: client"
CLIENT_ACCESS=$(echo "$HTTP_BODY" | jq -r '.data.access' 2>/dev/null)

# 8.1 Client cannot create service
if [ -n "$CLIENT_ACCESS" ] && [ "$CLIENT_ACCESS" != "null" ]; then
    http POST "/auth/services/" \
        '{"name": "Hack", "price": "100.00", "duration_minutes": 30}' \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Client cannot create service → 403" "403" "$HTTP_STATUS"
fi

# 8.2 Specialist can create service
if [ -n "$SPEC_ACCESS" ] && [ "$SPEC_ACCESS" != "null" ]; then
    http POST "/auth/services/" \
        '{"name": "Test Manicure", "price": "1500.00", "duration_minutes": 60}' \
        -H "X-App-Type: pro" \
        -H "Authorization: Bearer ${SPEC_ACCESS}"
    assert "Specialist creates service → 201" "201" "$HTTP_STATUS"
    SERVICE_ID=$(echo "$HTTP_BODY" | jq -r '.id // .data.id' 2>/dev/null)

    # 8.3 List services
    http GET "/auth/services/" "" \
        -H "X-App-Type: pro" \
        -H "Authorization: Bearer ${SPEC_ACCESS}"
    assert "List services → 200" "200" "$HTTP_STATUS"
fi

# 8.4 Unauthenticated access
http GET "/auth/services/" "" -H "X-App-Type: client"
assert "Services without auth → 401" "401" "$HTTP_STATUS"


# =============================================================================
# 9. EDGE CASES & SECURITY
# =============================================================================
echo ""
echo -e "${CYAN}[9] Edge cases & security${NC}"

# 9.1 Wrong HTTP method
response=$(curl -s -w '\n%{http_code}' -X GET "${API}/auth/register/" \
    -H "X-App-Type: client" 2>/dev/null)
status=$(echo "$response" | tail -1)
assert "GET on POST-only endpoint → 405" "405" "$status"

response=$(curl -s -w '\n%{http_code}' -X DELETE "${API}/auth/login/" \
    -H "X-App-Type: client" 2>/dev/null)
status=$(echo "$response" | tail -1)
assert "DELETE on login → 405" "405" "$status"

# 9.2 Non-existent endpoint
http GET "/auth/nonexistent/" "" -H "X-App-Type: client"
assert "Non-existent endpoint → 404" "404" "$HTTP_STATUS"

# 9.3 SQL injection attempt in phone
http POST "/auth/login/" '{"phone": "+7900123456; DROP TABLE users;"}' -H "X-App-Type: client"
assert "SQL injection in phone → 400 (not 500)" "400" "$HTTP_STATUS"

# 9.4 XSS attempt in phone
http POST "/auth/login/" '{"phone": "<script>alert(1)</script>"}' -H "X-App-Type: client"
assert "XSS in phone → 400" "400" "$HTTP_STATUS"

# 9.5 Very long phone
LONG_PHONE="+7$(printf '9%.0s' {1..200})"
http POST "/auth/register/" "{\"phone\": \"${LONG_PHONE}\"}" -H "X-App-Type: client"
assert "200-digit phone → 400 (not 500)" "400" "$HTTP_STATUS"

# 9.6 Unicode in phone
http POST "/auth/login/" '{"phone": "+7９００１２３４５６７"}' -H "X-App-Type: client"
assert_not "Unicode digits in phone → no 500" "500" "$HTTP_STATUS"

# 9.7 Null values
http POST "/auth/register/" '{"phone": null}' -H "X-App-Type: client"
assert "Null phone → 400" "400" "$HTTP_STATUS"

# 9.8 Extra fields are ignored (no error)
http POST "/auth/register/" "{\"phone\": \"+7900${RANDOM_SUFFIX}070\", \"admin\": true, \"role\": \"admin\"}" -H "X-App-Type: client"
assert "Extra fields don't cause 500" "201" "$HTTP_STATUS"

# 9.9 Content-Type variations
response=$(curl -s -w '\n%{http_code}' -X POST "${API}/auth/login/" \
    -H "X-App-Type: client" \
    -H "Content-Type: text/plain" \
    -d '{"phone": "+79001234567"}' 2>/dev/null)
status=$(echo "$response" | tail -1)
assert_not "text/plain Content-Type doesn't 500" "500" "$status"

# 9.10 Empty Content-Type
response=$(curl -s -w '\n%{http_code}' -X POST "${API}/auth/login/" \
    -H "X-App-Type: client" \
    -d '{"phone": "+79001234567"}' 2>/dev/null)
status=$(echo "$response" | tail -1)
assert_not "No Content-Type doesn't 500" "500" "$status"

# 9.11 Huge JSON body
BIG_BODY=$(python3 -c "import json; print(json.dumps({'phone': '+79001234567', 'extra': 'A' * 100000}))")
response=$(curl -s -w '\n%{http_code}' -X POST "${API}/auth/register/" \
    -H "Content-Type: application/json" \
    -H "X-App-Type: client" \
    -d "$BIG_BODY" 2>/dev/null)
status=$(echo "$response" | tail -1)
assert_not "100KB body doesn't 500" "500" "$status"


# =============================================================================
# 10. RATE LIMITING (OTP)
# =============================================================================
echo ""
echo -e "${CYAN}[10] OTP rate limiting${NC}"

# Register a new user, then try to send OTP immediately again
RATE_PHONE="+7900${RANDOM_SUFFIX}080"
http POST "/auth/register/" "{\"phone\": \"${RATE_PHONE}\"}" -H "X-App-Type: client"
assert "First OTP send → 201" "201" "$HTTP_STATUS"

# Immediately try login (which sends another OTP)
http POST "/auth/login/" "{\"phone\": \"${RATE_PHONE}\"}" -H "X-App-Type: client"
assert "Immediate re-send → 429 (rate limited)" "429" "$HTTP_STATUS"
assert_json_field "Error code = RATE_LIMITED" ".error.code" "RATE_LIMITED" "$HTTP_BODY"


# =============================================================================
# 11. RESPONSE FORMAT CONSISTENCY
# =============================================================================
echo ""
echo -e "${CYAN}[11] Response format consistency${NC}"

# 11.1 Success responses have "data" wrapper
http GET "/health/"
assert_contains "Health has correct JSON structure" '"status"' "$HTTP_BODY"

# 11.2 Error responses have "error" wrapper
http POST "/auth/login/" '{}' -H "X-App-Type: client"
assert_contains "Error response has error wrapper" '"error"' "$HTTP_BODY"
assert_contains "Error has code field" '"code"' "$HTTP_BODY"
assert_contains "Error has message field" '"message"' "$HTTP_BODY"

# 11.3 Validation errors include details
http POST "/auth/verify-otp/" '{}' -H "X-App-Type: client"
assert_contains "Validation error has details" '"details"' "$HTTP_BODY"


# =============================================================================
# RESULTS
# =============================================================================
echo ""
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
if [ "$FAILED" -eq 0 ]; then
    echo -e "${GREEN}  ALL TESTS PASSED: ${PASSED}/${TOTAL}${NC}"
else
    echo -e "${RED}  FAILED: ${FAILED}/${TOTAL}${NC}"
    echo -e "${GREEN}  PASSED: ${PASSED}/${TOTAL}${NC}"
    echo ""
    echo -e "${RED}  Failed tests:${FAILURES}${NC}"
fi
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
echo ""

exit "$FAILED"
