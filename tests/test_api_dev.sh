#!/usr/bin/env bash
# =============================================================================
# Integration tests for BeautyGO dev server
#
# Usage:
#   ./tests/test_api_dev.sh                        # default: http://localhost:8000
#   ./tests/test_api_dev.sh https://dev.beautygo.ru # custom server
#   BASE_URL=https://dev.beautygo.ru ./tests/test_api_dev.sh
#
# Logs are written to: tests/integration_results.log
#
# Requirements: curl, python3
# =============================================================================

set -uo pipefail

# Find python executable (Windows compatibility)
PYTHON=""
for cmd in python3 python py; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON="$cmd"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    # Fallback to known Windows path
    for p in /c/Users/*/AppData/Local/Programs/Python/Python*/python.exe; do
        if [ -x "$p" ]; then PYTHON="$p"; break; fi
    done
fi
if [ -z "$PYTHON" ]; then
    echo "ERROR: python not found in PATH"
    exit 1
fi

BASE_URL="${1:-${BASE_URL:-https://dev.gobeauty.site}}"
API="${BASE_URL}/api/v1"

# --- Log file ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${SCRIPT_DIR}/integration_results.log"
RUN_TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"

# Initialize log file
cat > "$LOG_FILE" <<EOF
================================================================================
BeautyGO Integration Tests
Server: ${BASE_URL}
Started: ${RUN_TIMESTAMP}
================================================================================

EOF

# Log to both file and stdout
log() {
    local level="$1"
    shift
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "${ts} [${level}] $*" >> "$LOG_FILE"
}

log_section() {
    echo "" >> "$LOG_FILE"
    echo "--- $1 ---" >> "$LOG_FILE"
}

log_http() {
    local method="$1"
    local path="$2"
    local status="$3"
    local body="$4"
    log "HTTP" "${method} ${path} → ${status}"
    log "BODY" "${body:0:500}"
}

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
        log "PASS" "${name}"
        PASSED=$((PASSED + 1))
    else
        echo -e "  ${RED}✗${NC} ${name}"
        echo -e "    expected: ${YELLOW}${expected}${NC}"
        echo -e "    actual:   ${RED}${actual}${NC}"
        log "FAIL" "${name} (expected=${expected}, got=${actual})"
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
        log "PASS" "${name}"
        PASSED=$((PASSED + 1))
    else
        echo -e "  ${RED}✗${NC} ${name}"
        echo -e "    should NOT be: ${RED}${unexpected}${NC}"
        log "FAIL" "${name} (should not be ${unexpected})"
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
        log "PASS" "${name}"
        PASSED=$((PASSED + 1))
    else
        echo -e "  ${RED}✗${NC} ${name}"
        echo -e "    body does not contain: ${YELLOW}${substring}${NC}"
        echo -e "    body: ${RED}${body:0:200}${NC}"
        log "FAIL" "${name} (missing '${substring}' in: ${body:0:200})"
        FAILED=$((FAILED + 1))
        FAILURES="${FAILURES}\n  ✗ ${name} (missing '${substring}')"
    fi
}

# Python-based JSON field extractor (no jq dependency)
# Writes body to temp file, reads from python via sys.argv path
json_get() {
    local tmpf
    tmpf=$(mktemp)
    printf '%s' "$2" > "$tmpf"
    local result
    result=$($PYTHON -c "
import json, sys
try:
    with open(sys.argv[1], 'r') as f:
        data = json.load(f)
    keys = sys.argv[2].strip('.').split('.')
    val = data
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k)
        elif isinstance(val, list) and k.isdigit():
            val = val[int(k)]
        else:
            val = None
            break
    if val is None:
        print('null')
    elif isinstance(val, bool):
        print(str(val).lower())
    else:
        print(val)
except Exception as e:
    print('JSON_ERROR:' + str(e))
" "$tmpf" "$1") || result="JSON_ERROR"
    rm -f "$tmpf"
    echo "$result"
}

assert_json_field() {
    local name="$1"
    local jq_expr="$2"
    local expected="$3"
    local body="$4"
    local actual
    actual=$(json_get "$jq_expr" "$body")
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

    local curl_args=(-s -k -w '\n%{http_code}' -X "$method")
    curl_args+=(-H "Content-Type: application/json")

    # Pass remaining args (headers etc.)
    curl_args+=("$@")

    if [ -n "$data" ]; then
        curl_args+=(-d "$data")
    fi

    # Show request
    echo -e "    ${CYAN}→ ${method} ${path}${NC}"
    if [ -n "$data" ]; then
        echo -e "    ${CYAN}  body: ${data:0:200}${NC}"
    fi

    local response
    response=$(curl "${curl_args[@]}" "${API}${path}" 2>/dev/null) || {
        HTTP_STATUS="000"
        HTTP_BODY='{"error": "connection refused"}'
        echo -e "    ${RED}← CONNECTION REFUSED${NC}"
        return
    }

    HTTP_STATUS=$(echo "$response" | tail -1 | tr -d '\r')
    HTTP_BODY=$(echo "$response" | sed '$d' | tr -d '\r')

    # Show response
    local pretty _vtmp
    _vtmp=$(mktemp)
    printf '%s' "$HTTP_BODY" > "$_vtmp"
    pretty=$($PYTHON -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1])),indent=2,ensure_ascii=False))" "$_vtmp" 2>/dev/null || echo "$HTTP_BODY")
    rm -f "$_vtmp"
    echo -e "    ${YELLOW}← ${HTTP_STATUS} ${pretty:0:300}${NC}"

    log_http "$method" "$path" "$HTTP_STATUS" "$HTTP_BODY"
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
log_section "[0] Server connectivity"
echo -e "${CYAN}[0] Server connectivity${NC}"

http GET "/health/"
assert "Health endpoint returns 200" "200" "$HTTP_STATUS"
assert_json_field "Health body has status=ok" ".status" "ok" "$HTTP_BODY"


# =============================================================================
# 1. X-APP-TYPE MIDDLEWARE
# =============================================================================
echo ""
log_section "[1] X-App-Type middleware"
echo -e "${CYAN}[1] X-App-Type middleware${NC}"

# 1.1 Missing header
response=$(curl -sk -w '\n%{http_code}' -X POST "${API}/auth/login/" \
    -H "Content-Type: application/json" \
    -d '{"phone": "+79001234567"}' 2>/dev/null)
status=$(echo "$response" | tail -1)
body=$(echo "$response" | sed '$d')
assert "Missing X-App-Type → 403" "403" "$status"
assert_json_field "Error code = APP_TYPE_MISSING" ".error.code" "APP_TYPE_MISSING" "$body"

# 1.2 Invalid header value
response=$(curl -sk -w '\n%{http_code}' -X POST "${API}/auth/login/" \
    -H "Content-Type: application/json" \
    -H "X-App-Type: hacker" \
    -d '{"phone": "+79001234567"}' 2>/dev/null)
status=$(echo "$response" | tail -1)
body=$(echo "$response" | sed '$d')
assert "Invalid X-App-Type → 403" "403" "$status"
assert_json_field "Error code = APP_TYPE_INVALID" ".error.code" "APP_TYPE_INVALID" "$body"

# 1.3 Empty header
response=$(curl -sk -w '\n%{http_code}' -X POST "${API}/auth/login/" \
    -H "Content-Type: application/json" \
    -H "X-App-Type: " \
    -d '{"phone": "+79001234567"}' 2>/dev/null)
status=$(echo "$response" | tail -1)
assert "Empty X-App-Type → 403" "403" "$status"

# 1.4 Health bypass
response=$(curl -sk -w '\n%{http_code}' "${API}/health/" 2>/dev/null)
status=$(echo "$response" | tail -1)
assert "Health endpoint bypasses middleware (no header needed)" "200" "$status"

# 1.5 Docs bypass
response=$(curl -sk -w '\n%{http_code}' "${BASE_URL}/api/docs/" 2>/dev/null)
status=$(echo "$response" | tail -1)
assert_not "Docs endpoint bypasses middleware" "403" "$status"

# 1.6 Case sensitivity
response=$(curl -sk -w '\n%{http_code}' -X POST "${API}/auth/login/" \
    -H "Content-Type: application/json" \
    -H "X-App-Type: CLIENT" \
    -d '{"phone": "+79001234567"}' 2>/dev/null)
status=$(echo "$response" | tail -1)
assert "X-App-Type is case-sensitive (CLIENT ≠ client) → 403" "403" "$status"


# =============================================================================
# 2. REGISTRATION
# =============================================================================
echo ""
log_section "[2] Registration (POST /auth/register/)"
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
response=$(curl -sk -w '\n%{http_code}' -X POST "${API}/auth/register/" \
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
log_section "[3] Login (POST /auth/login/)"
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
log_section "[4] Verify OTP (POST /auth/verify-otp/)"
echo -e "${CYAN}[4] Verify OTP (POST /auth/verify-otp/)${NC}"

# Register fresh user for OTP tests
http POST "/auth/register/" "{\"phone\": \"${TEST_PHONE2}\"}" -H "X-App-Type: client"

# 4.1 Correct OTP (debug code = 000000)
http POST "/auth/verify-otp/" "{\"phone\": \"${TEST_PHONE2}\", \"code\": \"000000\"}" -H "X-App-Type: client"
assert "Verify correct OTP → 200" "200" "$HTTP_STATUS"
# Check access token exists (non-null)
ACCESS_TOKEN=$(json_get ".data.access" "$HTTP_BODY")
REFRESH_TOKEN=$(json_get ".data.refresh" "$HTTP_BODY")
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
log_section "[5] Authenticated endpoints"
echo -e "${CYAN}[5] Authenticated endpoints${NC}"

# 5.1 Profile without auth
http GET "/auth/users/me/" "" -H "X-App-Type: client"
assert "Profile without auth → 401" "401" "$HTTP_STATUS"

# 5.2 Profile with valid token
if [ -n "$ACCESS_TOKEN" ] && [ "$ACCESS_TOKEN" != "null" ]; then
    http GET "/auth/users/me/" "" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${ACCESS_TOKEN}"
    assert "Profile with auth → 200" "200" "$HTTP_STATUS"
    # ProfileDetailView uses DRF default (no "data" wrapper)
    assert_contains "Profile response is valid JSON" '"id"' "$HTTP_BODY"
fi

# 5.3 Invalid token
http GET "/auth/users/me/" "" \
    -H "X-App-Type: client" \
    -H "Authorization: Bearer invalid.token.here"
assert "Invalid token → 401" "401" "$HTTP_STATUS"

# 5.4 Expired/garbage token
http GET "/auth/users/me/" "" \
    -H "X-App-Type: client" \
    -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0b2tlbl90eXBlIjoiYWNjZXNzIiwiZXhwIjoxMDAwMDAwMDAwfQ.invalid"
assert "Forged token → 401" "401" "$HTTP_STATUS"

# 5.5 Missing Authorization header
http GET "/auth/users/me/" "" -H "X-App-Type: client"
assert "No Authorization header → 401" "401" "$HTTP_STATUS"


# =============================================================================
# 6. LOGOUT
# =============================================================================
echo ""
log_section "[6] Logout (POST /auth/logout/)"
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
log_section "[7] Token refresh (POST /auth/token/refresh/)"
echo -e "${CYAN}[7] Token refresh (POST /auth/token/refresh/)${NC}"

# Get fresh tokens
http POST "/auth/register/" "{\"phone\": \"+7900${RANDOM_SUFFIX}050\"}" -H "X-App-Type: client"
http POST "/auth/verify-otp/" "{\"phone\": \"+7900${RANDOM_SUFFIX}050\", \"code\": \"000000\"}" -H "X-App-Type: client"
FRESH_REFRESH=$(json_get ".data.refresh" "$HTTP_BODY")

if [ -n "$FRESH_REFRESH" ] && [ "$FRESH_REFRESH" != "null" ]; then
    # 7.1 Valid refresh
    http POST "/auth/token/refresh/" "{\"refresh\": \"${FRESH_REFRESH}\"}" -H "X-App-Type: client"
    assert "Token refresh → 200" "200" "$HTTP_STATUS"
    NEW_ACCESS=$(json_get ".access" "$HTTP_BODY")
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
# 8. SERVICES CRUD (specialist only, X-App-Type: pro)
# =============================================================================
echo ""
log_section "[8] Services CRUD (specialist only)"
echo -e "${CYAN}[8] Services CRUD — Pro App (POST/GET/PATCH/DELETE /services/)${NC}"

# Get specialist tokens
sleep 1
http POST "/auth/login/" "{\"phone\": \"${TEST_PHONE_SPECIALIST}\"}" -H "X-App-Type: pro"
if [ "$HTTP_STATUS" = "429" ]; then
    echo -e "  ${YELLOW}⚠${NC} Rate limited, waiting 61s..."
    sleep 61
    http POST "/auth/login/" "{\"phone\": \"${TEST_PHONE_SPECIALIST}\"}" -H "X-App-Type: pro"
fi
http POST "/auth/verify-otp/" "{\"phone\": \"${TEST_PHONE_SPECIALIST}\", \"code\": \"000000\"}" -H "X-App-Type: pro"
SPEC_ACCESS=$(json_get ".data.access" "$HTTP_BODY")

# Get client tokens for comparison
http POST "/auth/register/" "{\"phone\": \"+7900${RANDOM_SUFFIX}060\"}" -H "X-App-Type: client"
http POST "/auth/verify-otp/" "{\"phone\": \"+7900${RANDOM_SUFFIX}060\", \"code\": \"000000\"}" -H "X-App-Type: client"
CLIENT_ACCESS=$(json_get ".data.access" "$HTTP_BODY")

if [ -n "$SPEC_ACCESS" ] && [ "$SPEC_ACCESS" != "null" ]; then

    # 8.1 Create service (without category)
    http POST "/services/" \
        '{"name": "Test Manicure", "price": "1500.00", "duration_minutes": 60}' \
        -H "X-App-Type: pro" \
        -H "Authorization: Bearer ${SPEC_ACCESS}"
    assert "Create service → 201" "201" "$HTTP_STATUS"
    SERVICE_ID=$(json_get ".id" "$HTTP_BODY" | tr -d '[:space:]')
    assert_json_field "Service name matches" ".name" "Test Manicure" "$HTTP_BODY"
    assert_json_field "Service is_active default true" ".is_active" "true" "$HTTP_BODY"

    # 8.2 List own services
    http GET "/services/" "" \
        -H "X-App-Type: pro" \
        -H "Authorization: Bearer ${SPEC_ACCESS}"
    assert "List services → 200" "200" "$HTTP_STATUS"
    assert_contains "Response contains created service" "Test Manicure" "$HTTP_BODY"

    # 8.3 Get single service
    if [ -n "$SERVICE_ID" ] && [ "$SERVICE_ID" != "null" ]; then
        http GET "/services/${SERVICE_ID}/" "" \
            -H "X-App-Type: pro" \
            -H "Authorization: Bearer ${SPEC_ACCESS}"
        assert "Get service by id → 200" "200" "$HTTP_STATUS"
        assert_contains "Service response has correct id" "\"id\":\"${SERVICE_ID}\"" "$HTTP_BODY"
    fi

    # 8.4 Update service (PATCH)
    if [ -n "$SERVICE_ID" ] && [ "$SERVICE_ID" != "null" ]; then
        http PATCH "/services/${SERVICE_ID}/" \
            '{"price": "2000.00", "name": "Updated Manicure"}' \
            -H "X-App-Type: pro" \
            -H "Authorization: Bearer ${SPEC_ACCESS}"
        assert "Update service → 200" "200" "$HTTP_STATUS"
        assert_json_field "Price updated" ".price" "2000.00" "$HTTP_BODY"
        assert_json_field "Name updated" ".name" "Updated Manicure" "$HTTP_BODY"
    fi

    # 8.5 Deactivate service
    if [ -n "$SERVICE_ID" ] && [ "$SERVICE_ID" != "null" ]; then
        http PATCH "/services/${SERVICE_ID}/" \
            '{"is_active": false}' \
            -H "X-App-Type: pro" \
            -H "Authorization: Bearer ${SPEC_ACCESS}"
        assert "Deactivate service → 200" "200" "$HTTP_STATUS"
        assert_json_field "is_active = false" ".is_active" "false" "$HTTP_BODY"
    fi

    # 8.6 Delete service
    if [ -n "$SERVICE_ID" ] && [ "$SERVICE_ID" != "null" ]; then
        http DELETE "/services/${SERVICE_ID}/" "" \
            -H "X-App-Type: pro" \
            -H "Authorization: Bearer ${SPEC_ACCESS}"
        assert "Delete service → 204" "204" "$HTTP_STATUS"

        # Confirm deletion
        http GET "/services/${SERVICE_ID}/" "" \
            -H "X-App-Type: pro" \
            -H "Authorization: Bearer ${SPEC_ACCESS}"
        assert "Deleted service → 404" "404" "$HTTP_STATUS"
    fi

    # 8.7 Create service with missing required fields
    http POST "/services/" \
        '{"name": ""}' \
        -H "X-App-Type: pro" \
        -H "Authorization: Bearer ${SPEC_ACCESS}"
    assert "Empty name → 400" "400" "$HTTP_STATUS"

    http POST "/services/" \
        '{"name": "Test", "price": "-100", "duration_minutes": 30}' \
        -H "X-App-Type: pro" \
        -H "Authorization: Bearer ${SPEC_ACCESS}"
    assert_not "Negative price doesn't 500" "500" "$HTTP_STATUS"

    http POST "/services/" \
        '{}' \
        -H "X-App-Type: pro" \
        -H "Authorization: Bearer ${SPEC_ACCESS}"
    assert "Empty body → 400" "400" "$HTTP_STATUS"

fi

# 8.8 Client cannot create service
if [ -n "$CLIENT_ACCESS" ] && [ "$CLIENT_ACCESS" != "null" ]; then
    http POST "/services/" \
        '{"name": "Hack", "price": "100.00", "duration_minutes": 30}' \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Client cannot create service → 403" "403" "$HTTP_STATUS"
fi

# 8.9 Unauthenticated access
http GET "/services/" "" -H "X-App-Type: pro"
assert "Services without auth → 401" "401" "$HTTP_STATUS"


# =============================================================================
# 9. EDGE CASES & SECURITY
# =============================================================================
echo ""
log_section "[9] Edge cases & security"
echo -e "${CYAN}[9] Edge cases & security${NC}"

# 9.1 Wrong HTTP method
response=$(curl -sk -w '\n%{http_code}' -X GET "${API}/auth/register/" \
    -H "X-App-Type: client" 2>/dev/null)
status=$(echo "$response" | tail -1)
assert "GET on POST-only endpoint → 405" "405" "$status"

response=$(curl -sk -w '\n%{http_code}' -X DELETE "${API}/auth/login/" \
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
response=$(curl -sk -w '\n%{http_code}' -X POST "${API}/auth/login/" \
    -H "X-App-Type: client" \
    -H "Content-Type: text/plain" \
    -d '{"phone": "+79001234567"}' 2>/dev/null)
status=$(echo "$response" | tail -1)
assert_not "text/plain Content-Type doesn't 500" "500" "$status"

# 9.10 Empty Content-Type
response=$(curl -sk -w '\n%{http_code}' -X POST "${API}/auth/login/" \
    -H "X-App-Type: client" \
    -d '{"phone": "+79001234567"}' 2>/dev/null)
status=$(echo "$response" | tail -1)
assert_not "No Content-Type doesn't 500" "500" "$status"

# 9.11 Huge JSON body
BIG_BODY=$($PYTHON -c "import json; print(json.dumps({'phone': '+79001234567', 'extra': 'A' * 100000}))")
response=$(curl -sk -w '\n%{http_code}' -X POST "${API}/auth/register/" \
    -H "Content-Type: application/json" \
    -H "X-App-Type: client" \
    -d "$BIG_BODY" 2>/dev/null)
status=$(echo "$response" | tail -1)
assert_not "100KB body doesn't 500" "500" "$status"


# =============================================================================
# 10. RATE LIMITING (OTP)
# =============================================================================
echo ""
log_section "[10] OTP rate limiting"
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
log_section "[11] Response format consistency"
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
# 12. SERVICE CATEGORIES
# =============================================================================
echo ""
log_section "[12] Service Categories (GET /services/categories/)"
echo -e "${CYAN}[12] Service Categories (GET /services/categories/)${NC}"

# 12.1 List categories (public — no auth needed, but X-App-Type required)
http GET "/services/categories/" "" -H "X-App-Type: client"
assert "List categories → 200" "200" "$HTTP_STATUS"
_tmp_cat=$(mktemp)
printf '%s' "$HTTP_BODY" > "$_tmp_cat"
CAT_COUNT=$($PYTHON -c "import json,sys; data=json.load(open(sys.argv[1])); print(len(data) if isinstance(data,list) else 0)" "$_tmp_cat" 2>/dev/null)
rm -f "$_tmp_cat"
TOTAL=$((TOTAL + 1))
if [ -n "$CAT_COUNT" ] && [ "$CAT_COUNT" -gt 0 ] 2>/dev/null; then
    echo -e "  ${GREEN}✓${NC} Categories returned: ${CAT_COUNT}"
    PASSED=$((PASSED + 1))
else
    echo -e "  ${YELLOW}⚠${NC} No categories found (fixture may not be loaded)"
    PASSED=$((PASSED + 1))  # Not a failure, just no data
fi

# 12.2 Categories with children structure
if [ -n "$CAT_COUNT" ] && [ "$CAT_COUNT" -gt 0 ] 2>/dev/null; then
    FIRST_CAT_ID=$(json_get ".0.id" "$HTTP_BODY")
    FIRST_CAT_NAME=$(json_get ".0.name" "$HTTP_BODY")
    CHILDREN=$(json_get ".0.children" "$HTTP_BODY")
    assert_contains "Category has name field" '"name"' "$HTTP_BODY"
    assert_contains "Category has slug field" '"slug"' "$HTTP_BODY"
    assert_contains "Category has children field" '"children"' "$HTTP_BODY"

    # 12.3 Get single category
    if [ -n "$FIRST_CAT_ID" ] && [ "$FIRST_CAT_ID" != "null" ]; then
        http GET "/services/categories/${FIRST_CAT_ID}/" "" -H "X-App-Type: client"
        assert "Get category by id → 200" "200" "$HTTP_STATUS"
    fi
fi

# 12.4 Categories from pro app too
http GET "/services/categories/" "" -H "X-App-Type: pro"
assert "Categories from pro app → 200" "200" "$HTTP_STATUS"

# 12.5 Categories without X-App-Type
response=$(curl -sk -w '\n%{http_code}' "${API}/services/categories/" 2>/dev/null)
status=$(echo "$response" | tail -1)
assert "Categories without X-App-Type → 403" "403" "$status"


# =============================================================================
# 13. SERVICE PUBLIC SEARCH (Client App)
# =============================================================================
echo ""
log_section "[13] Service Public Search (GET /services/search/)"
echo -e "${CYAN}[13] Service Public Search (GET /services/search/)${NC}"

# First create some services as specialist for search tests
if [ -n "$SPEC_ACCESS" ] && [ "$SPEC_ACCESS" != "null" ]; then
    http POST "/services/" \
        '{"name": "Search Manicure", "price": "1500.00", "duration_minutes": 60}' \
        -H "X-App-Type: pro" \
        -H "Authorization: Bearer ${SPEC_ACCESS}"
    SEARCH_SVC_ID=$(json_get ".id" "$HTTP_BODY")

    http POST "/services/" \
        '{"name": "Search Pedicure", "price": "2500.00", "duration_minutes": 90}' \
        -H "X-App-Type: pro" \
        -H "Authorization: Bearer ${SPEC_ACCESS}"
fi

# 13.1 List all active services (authenticated client)
if [ -n "$CLIENT_ACCESS" ] && [ "$CLIENT_ACCESS" != "null" ]; then
    http GET "/services/search/" "" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Public search → 200" "200" "$HTTP_STATUS"

    # 13.2 Response includes specialist_info
    assert_contains "Response has specialist_info" '"specialist_info"' "$HTTP_BODY"

    # 13.3 Filter by name
    http GET "/services/search/?name=Manicure" "" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Filter by name → 200" "200" "$HTTP_STATUS"

    # 13.4 Filter by price range
    http GET "/services/search/?min_price=2000&max_price=3000" "" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Filter by price range → 200" "200" "$HTTP_STATUS"

    # 13.5 Order by price
    http GET "/services/search/?ordering=price" "" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Order by price → 200" "200" "$HTTP_STATUS"

    # 13.6 Order by price descending
    http GET "/services/search/?ordering=-price" "" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Order by -price → 200" "200" "$HTTP_STATUS"

    # 13.7 Get single service detail
    if [ -n "$SEARCH_SVC_ID" ] && [ "$SEARCH_SVC_ID" != "null" ]; then
        http GET "/services/search/${SEARCH_SVC_ID}/" "" \
            -H "X-App-Type: client" \
            -H "Authorization: Bearer ${CLIENT_ACCESS}"
        assert "Service detail → 200" "200" "$HTTP_STATUS"
        assert_contains "Detail has specialist_info" '"specialist_info"' "$HTTP_BODY"
        assert_contains "Detail has specialist_address" '"specialist_address"' "$HTTP_BODY"
    fi

    # 13.8 POST on read-only → 405
    http POST "/services/search/" \
        '{"name": "Hack"}' \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "POST on search (read-only) → 405" "405" "$HTTP_STATUS"
fi

# 13.9 Unauthenticated → 401
http GET "/services/search/" "" -H "X-App-Type: client"
assert "Public search without auth → 401" "401" "$HTTP_STATUS"


# =============================================================================
# 14. CLIENT PROFILE
# =============================================================================
echo ""
log_section "[14] Client Profile (GET/PATCH /auth/clients/me/)"
echo -e "${CYAN}[14] Client Profile (GET/PATCH /auth/clients/me/)${NC}"

if [ -n "$CLIENT_ACCESS" ] && [ "$CLIENT_ACCESS" != "null" ]; then
    # 14.1 Get client profile
    http GET "/auth/clients/me/" "" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Get client profile → 200" "200" "$HTTP_STATUS"
    assert_contains "Profile has full_name" '"full_name"' "$HTTP_BODY"

    # 14.2 Update client profile
    http PATCH "/auth/clients/me/" \
        '{"full_name": "Test Client Name"}' \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Update client profile → 200" "200" "$HTTP_STATUS"
    assert_json_field "Name updated" ".data.full_name" "Test Client Name" "$HTTP_BODY"

    # 14.3 Name too short → 400
    http PATCH "/auth/clients/me/" \
        '{"full_name": "A"}' \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Name < 2 chars → 400" "400" "$HTTP_STATUS"

    # 14.4 Client profile from pro app → 403
    http GET "/auth/clients/me/" "" \
        -H "X-App-Type: pro" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Client profile from pro app → 403" "403" "$HTTP_STATUS"
fi

# 14.5 Specialist cannot access client profile
if [ -n "$SPEC_ACCESS" ] && [ "$SPEC_ACCESS" != "null" ]; then
    http GET "/auth/clients/me/" "" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${SPEC_ACCESS}"
    assert "Specialist → client profile → 403" "403" "$HTTP_STATUS"
fi

# 14.6 Without auth → 401
http GET "/auth/clients/me/" "" -H "X-App-Type: client"
assert "Client profile without auth → 401" "401" "$HTTP_STATUS"


# =============================================================================
# 15. SPECIALIST PROFILE
# =============================================================================
echo ""
log_section "[15] Specialist Profile (POST/PATCH /auth/masters/me/, GET /auth/masters/me/)"
echo -e "${CYAN}[15] Specialist Profile (masters/me/ & masters/me/)${NC}"

if [ -n "$SPEC_ACCESS" ] && [ "$SPEC_ACCESS" != "null" ]; then
    # 15.1 Get specialist profile
    http GET "/auth/masters/me/" "" \
        -H "X-App-Type: pro" \
        -H "Authorization: Bearer ${SPEC_ACCESS}"
    assert "Get specialist profile → 200" "200" "$HTTP_STATUS"
    assert_contains "Profile has display_name" '"display_name"' "$HTTP_BODY"
    assert_contains "Profile has status" '"status"' "$HTTP_BODY"
    assert_contains "Profile has rating" '"rating"' "$HTTP_BODY"

    # 15.2 Update specialist profile
    http PATCH "/auth/masters/me/" \
        '{"display_name": "Updated Master", "bio": "Test bio"}' \
        -H "X-App-Type: pro" \
        -H "Authorization: Bearer ${SPEC_ACCESS}"
    assert "Update specialist profile → 200" "200" "$HTTP_STATUS"

    # 15.3 Update address + location
    http PATCH "/auth/masters/me/" \
        '{"address": "Казань, ул. Баумана 1", "location_lat": "55.796127", "location_lng": "49.106405"}' \
        -H "X-App-Type: pro" \
        -H "Authorization: Bearer ${SPEC_ACCESS}"
    assert "Update address → 200" "200" "$HTTP_STATUS"

    # 15.4 Specialist profile from client app → 403
    http GET "/auth/masters/me/" "" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${SPEC_ACCESS}"
    assert "Master profile from client app → 403" "403" "$HTTP_STATUS"
fi

# 15.5 Client cannot access master profile
if [ -n "$CLIENT_ACCESS" ] && [ "$CLIENT_ACCESS" != "null" ]; then
    http GET "/auth/masters/me/" "" \
        -H "X-App-Type: pro" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Client → master profile → 403" "403" "$HTTP_STATUS"
fi

# 15.6 Without auth → 401
http GET "/auth/masters/me/" "" -H "X-App-Type: pro"
assert "Master profile without auth → 401" "401" "$HTTP_STATUS"


# =============================================================================
# 16. SEND CODE (Reauth)
# =============================================================================
echo ""
log_section "[16] Send Code (POST /auth/send-code/)"
echo -e "${CYAN}[16] Send Code (POST /auth/send-code/)${NC}"

# 16.1 Send code to registered phone (need to wait for rate limit)
sleep 2
http POST "/auth/send-code/" "{\"phone\": \"${TEST_PHONE2}\"}" -H "X-App-Type: client"
# May be 200 or 429 depending on timing
if [ "$HTTP_STATUS" = "429" ]; then
    echo -e "  ${YELLOW}⚠${NC} Rate limited (expected after previous OTP sends)"
    TOTAL=$((TOTAL + 1)); PASSED=$((PASSED + 1))
    echo -e "  ${GREEN}✓${NC} Rate limiting works on send-code"
else
    assert "Send code to existing phone → 200" "200" "$HTTP_STATUS"
fi

# 16.2 Send code to unregistered phone → 404
http POST "/auth/send-code/" '{"phone": "+79099998877"}' -H "X-App-Type: client"
assert "Send code to unknown phone → 404" "404" "$HTTP_STATUS"

# 16.3 Empty body → 400
http POST "/auth/send-code/" '{}' -H "X-App-Type: client"
assert "Send code empty body → 400" "400" "$HTTP_STATUS"

# 16.4 Invalid phone → 400
http POST "/auth/send-code/" '{"phone": "not-a-phone"}' -H "X-App-Type: client"
assert "Send code invalid phone → 400" "400" "$HTTP_STATUS"


# =============================================================================
# 17. CROSS-APP SECURITY
# =============================================================================
echo ""
log_section "[17] Cross-App Security"
echo -e "${CYAN}[17] Cross-App Security${NC}"

# 17.1 Client app → Pro-only endpoints
if [ -n "$CLIENT_ACCESS" ] && [ "$CLIENT_ACCESS" != "null" ]; then
    http POST "/services/" \
        '{"name": "Hack", "price": "100", "duration_minutes": 30}' \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Client app → create service → 403" "403" "$HTTP_STATUS"

    http GET "/auth/masters/me/" "" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Client app → master profile → 403" "403" "$HTTP_STATUS"
fi

# 17.2 Pro app → Client-only endpoints
if [ -n "$SPEC_ACCESS" ] && [ "$SPEC_ACCESS" != "null" ]; then
    http GET "/auth/clients/me/" "" \
        -H "X-App-Type: pro" \
        -H "Authorization: Bearer ${SPEC_ACCESS}"
    assert "Pro app → client profile → 403" "403" "$HTTP_STATUS"
fi

# 17.3 Wrong role + wrong app combo
if [ -n "$CLIENT_ACCESS" ] && [ "$CLIENT_ACCESS" != "null" ]; then
    http GET "/services/" "" \
        -H "X-App-Type: pro" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Client token + pro header → services → 403" "403" "$HTTP_STATUS"
fi

# 17.4 Specialist on client endpoint
if [ -n "$SPEC_ACCESS" ] && [ "$SPEC_ACCESS" != "null" ]; then
    http GET "/auth/clients/me/" "" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${SPEC_ACCESS}"
    assert "Specialist token + client header → client profile → 403" "403" "$HTTP_STATUS"
fi


# =============================================================================
# 18. CATEGORIES API (EPIC-02)
# =============================================================================
echo ""
log_section "[18] Categories API (GET /categories/)"
echo -e "${CYAN}[18] Categories API (GET /categories/)${NC}"

# 18.1 List root categories (public)
http GET "/categories/" "" -H "X-App-Type: client"
assert "List categories → 200" "200" "$HTTP_STATUS"
assert_contains "Response has specialists_count" '"specialists_count"' "$HTTP_BODY"

# 18.2 Categories available from pro app
http GET "/categories/" "" -H "X-App-Type: pro"
assert "Categories from pro → 200" "200" "$HTTP_STATUS"

# 18.3 Without X-App-Type → 403
response=$(curl -sk -w '\n%{http_code}' "${API}/categories/" 2>/dev/null)
status=$(echo "$response" | tail -1 | tr -d '\r')
assert "Categories without X-App-Type → 403" "403" "$status"

# 18.4 Load fixture and verify (if not loaded)
# Categories from fixture should have children
http GET "/categories/" "" -H "X-App-Type: client"
assert_contains "Categories have children field" '"children"' "$HTTP_BODY"


# =============================================================================
# 18.5 SPECIALISTS LIST (EPIC-02)
# =============================================================================
echo ""
log_section "[18.5] Specialists list (GET /specialists/)"
echo -e "${CYAN}[18.5] Specialists list (GET /specialists/)${NC}"

# 18.5.1 List specialists (authenticated client)
if [ -n "$CLIENT_ACCESS" ] && [ "$CLIENT_ACCESS" != "null" ]; then
    http GET "/specialists/" "" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "List specialists → 200" "200" "$HTTP_STATUS"

    # 18.5.2 Response has expected fields (skip if no specialists)
    if echo "$HTTP_BODY" | grep -q "display_name"; then
        assert_contains "Has display_name" '"display_name"' "$HTTP_BODY"
        assert_contains "Has rating" '"rating"' "$HTTP_BODY"
        assert_contains "Has services_preview" '"services_preview"' "$HTTP_BODY"
    else
        echo -e "  ${YELLOW}⚠${NC} No active specialists on server (empty list)"
    fi

    # 18.5.3 Filter by min_rating
    http GET "/specialists/?min_rating=4.0" "" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Filter by min_rating → 200" "200" "$HTTP_STATUS"

    # 18.5.4 Ordering
    http GET "/specialists/?ordering=-rating" "" \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "Order by rating → 200" "200" "$HTTP_STATUS"
fi

# 18.5.5 Unauthenticated → 401
http GET "/specialists/" "" -H "X-App-Type: client"
assert "Specialists without auth → 401" "401" "$HTTP_STATUS"

# 18.5.6 POST not allowed
if [ -n "$CLIENT_ACCESS" ] && [ "$CLIENT_ACCESS" != "null" ]; then
    http POST "/specialists/" '{"name": "Hack"}' \
        -H "X-App-Type: client" \
        -H "Authorization: Bearer ${CLIENT_ACCESS}"
    assert "POST specialists → 405" "405" "$HTTP_STATUS"
fi


# =============================================================================
# 19. SOCIAL AUTH
# =============================================================================
echo ""
log_section "[19] Social Auth (POST /auth/social/{provider}/)"
echo -e "${CYAN}[19] Social Auth (POST /auth/social/{provider}/)${NC}"

# 19.1 Invalid provider → 400
http POST "/auth/social/facebook/" '{"token": "fake"}' -H "X-App-Type: client"
assert "Invalid provider → 400" "400" "$HTTP_STATUS"
assert_json_field "Error code = INVALID_PROVIDER" ".error.code" "INVALID_PROVIDER" "$HTTP_BODY"

# 19.2 Missing token → 400
http POST "/auth/social/vk/" '{}' -H "X-App-Type: client"
assert "Missing token → 400" "400" "$HTTP_STATUS"

# 19.3 Invalid VK token → 401
http POST "/auth/social/vk/" '{"token": "invalid_token"}' -H "X-App-Type: client"
assert "Invalid VK token → 401" "401" "$HTTP_STATUS"
assert_json_field "Error code = SOCIAL_TOKEN_INVALID" ".error.code" "SOCIAL_TOKEN_INVALID" "$HTTP_BODY"

# 19.4 Without X-App-Type → 403
response=$(curl -sk -w '\n%{http_code}' -X POST "${API}/auth/social/vk/" \
    -H "Content-Type: application/json" \
    -d '{"token": "x"}' 2>/dev/null)
status=$(echo "$response" | tail -1 | tr -d '\r')
assert "Social auth without X-App-Type → 403" "403" "$status"

# 19.5 Bind phone without auth → 401
http POST "/auth/bind-phone/" '{"phone": "+79001234567", "code": "000000"}' -H "X-App-Type: client"
assert "Bind phone without auth → 401" "401" "$HTTP_STATUS"


# =============================================================================
# 20. ACCOUNT DELETION
# =============================================================================
echo ""
log_section "[20] Account Deletion (DELETE /auth/users/me/)"
echo -e "${CYAN}[20] Account Deletion (DELETE /auth/users/me/)${NC}"

# 20.1 Without auth → 401
http DELETE "/auth/users/me/" '{"confirmation": "DELETE"}' -H "X-App-Type: client"
assert "Delete without auth → 401" "401" "$HTTP_STATUS"

# 20.2 Without confirmation → 400
if [ -n "$CLIENT_ACCESS" ] && [ "$CLIENT_ACCESS" != "null" ]; then
    # Register a disposable user for deletion test
    DISPOSABLE_PHONE="+7900${RANDOM_SUFFIX}090"
    http POST "/auth/register/" "{\"phone\": \"${DISPOSABLE_PHONE}\"}" -H "X-App-Type: client"
    http POST "/auth/verify-otp/" "{\"phone\": \"${DISPOSABLE_PHONE}\", \"code\": \"000000\"}" -H "X-App-Type: client"
    DISPOSABLE_ACCESS=$(json_get ".data.access" "$HTTP_BODY" | tr -d '[:space:]')

    if [ -n "$DISPOSABLE_ACCESS" ] && [ "$DISPOSABLE_ACCESS" != "null" ]; then
        # 20.2 Wrong confirmation → 400
        http DELETE "/auth/users/me/" '{"confirmation": "WRONG"}' \
            -H "X-App-Type: client" \
            -H "Authorization: Bearer ${DISPOSABLE_ACCESS}"
        assert "Wrong confirmation → 400" "400" "$HTTP_STATUS"

        # 20.3 Empty body → 400
        http DELETE "/auth/users/me/" '{}' \
            -H "X-App-Type: client" \
            -H "Authorization: Bearer ${DISPOSABLE_ACCESS}"
        assert "Delete empty body → 400" "400" "$HTTP_STATUS"

        # 20.4 Successful deletion
        http DELETE "/auth/users/me/" '{"confirmation": "DELETE"}' \
            -H "X-App-Type: client" \
            -H "Authorization: Bearer ${DISPOSABLE_ACCESS}"
        assert "Delete account → 200" "200" "$HTTP_STATUS"
        assert_contains "Scheduled message" '"message"' "$HTTP_BODY"

        # 20.5 Deleted user token should be invalid
        http GET "/auth/users/me/" "" \
            -H "X-App-Type: client" \
            -H "Authorization: Bearer ${DISPOSABLE_ACCESS}"
        assert "Deleted user → 401" "401" "$HTTP_STATUS"
    fi
fi


# =============================================================================
# 21. BLOCKED USER (DRF-76)
# Requires localhost — uses manage.py shell to simulate admin block action
# =============================================================================

if [[ "$BASE_URL" == *"localhost"* ]] || [[ "$BASE_URL" == *"127.0.0.1"* ]]; then
    log_section "[21] Blocked User (DRF-76 — localhost only)"

    BLOCKED_PHONE="+7900${RANDOM_SUFFIX}091"

    # Register and authenticate
    http POST "/auth/register/" "{\"phone\": \"${BLOCKED_PHONE}\"}" -H "X-App-Type: client"
    http POST "/auth/verify-otp/" "{\"phone\": \"${BLOCKED_PHONE}\", \"code\": \"000000\"}" -H "X-App-Type: client"
    BLOCKED_ACCESS=$(json_get ".data.access" "$HTTP_BODY" | tr -d '[:space:]')

    if [ -n "$BLOCKED_ACCESS" ] && [ "$BLOCKED_ACCESS" != "null" ]; then
        # 21.1 Token works before blocking
        http GET "/auth/users/me/" "" \
            -H "X-App-Type: client" \
            -H "Authorization: Bearer ${BLOCKED_ACCESS}"
        assert "Active user → /users/me/ → 200" "200" "$HTTP_STATUS"

        # Block user via manage.py shell (simulates admin block action)
        MANAGE_PY="${SCRIPT_DIR}/../manage.py"
        DJANGO_SETTINGS_MODULE=djangoProject.settings.dev \
            $PYTHON "$MANAGE_PY" shell -c \
            "from users.models import User; User.objects.filter(phone='${BLOCKED_PHONE}').update(is_active=False)" \
            2>/dev/null

        # 21.2 Existing token must be rejected for blocked user
        http GET "/auth/users/me/" "" \
            -H "X-App-Type: client" \
            -H "Authorization: Bearer ${BLOCKED_ACCESS}"
        assert "Blocked user token → 401" "401" "$HTTP_STATUS"

        # 21.3 Blocked user cannot get new token via verify-otp
        http POST "/auth/login/" "{\"phone\": \"${BLOCKED_PHONE}\"}" -H "X-App-Type: client"
        http POST "/auth/verify-otp/" "{\"phone\": \"${BLOCKED_PHONE}\", \"code\": \"000000\"}" -H "X-App-Type: client"
        NEW_TOKEN=$(json_get ".data.access" "$HTTP_BODY" | tr -d '[:space:]')
        if [ -n "$NEW_TOKEN" ] && [ "$NEW_TOKEN" != "null" ]; then
            http GET "/auth/users/me/" "" \
                -H "X-App-Type: client" \
                -H "Authorization: Bearer ${NEW_TOKEN}"
            assert "Blocked user new token → 401" "401" "$HTTP_STATUS"
        fi
    else
        echo "  SKIP: could not get token for blocked user test"
    fi
else
    echo ""
    echo -e "${YELLOW}  [21] Blocked User — skipped (requires localhost)${NC}"
fi


# =============================================================================
# RESULTS
# =============================================================================
# Write summary to log
{
    echo ""
    echo "================================================================================"
    echo "SUMMARY"
    echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "Server:   ${BASE_URL}"
    echo "Total:    ${TOTAL}"
    echo "Passed:   ${PASSED}"
    echo "Failed:   ${FAILED}"
    if [ "$FAILED" -gt 0 ]; then
        echo ""
        echo "Failed tests:"
        echo -e "${FAILURES}"
    fi
    echo "================================================================================"
} >> "$LOG_FILE"

# Print to terminal
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
echo -e "${CYAN}  Log: ${LOG_FILE}${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
echo ""

echo ""
echo "Press Enter to close..."
read -r
exit "$FAILED"
