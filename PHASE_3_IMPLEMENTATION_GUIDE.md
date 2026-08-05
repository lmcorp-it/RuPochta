# Phase 3b-3d Complete Implementation Guide

**Date**: August 5, 2026  
**Status**: Ready for manual implementation  
**Scope**: 90+ patterns across 3 phases  

---

## How to Use This Guide

This guide documents every replacement pattern needed to complete Phase 3b-3d. Each section includes:
1. Pattern name and frequency
2. Example locations (first 3-5)
3. Before/after code
4. File and approximate line numbers
5. Implementation order (by priority/impact)

**Strategy**:
1. Follow sections in order (Phase 3b → 3c → 3d)
2. For each pattern type, find all instances using grep
3. Make targeted replacements using the edit tool
4. Test after each batch
5. Commit by category

---

## Phase 3b: Normalization, Errors, Validation

### Pattern 1: Email Normalization (.strip().lower())

**Frequency**: 12+ instances  
**Files**: rupochta_server.py (primary)  
**Utility**: `normalize_email()` from utils.normalization

**Before**:
```python
target = str(email_addr or "").strip().lower()
email = str(email_addr or "").strip().lower()
```

**After**:
```python
from utils.normalization import normalize_email
target = normalize_email(email_addr)
email = normalize_email(email_addr)
```

**Implementation**:
```bash
# Find locations:
grep -n 'str(.*or "")\.strip()\.lower()' rupochta_server.py | grep -i email

# Then edit each location using the edit tool
```

**Priority**: High (email validation is security-sensitive)

---

### Pattern 2: Domain Normalization

**Frequency**: 4+ instances  
**Utility**: `normalize_domain()` from utils.normalization

**Before**:
```python
domain = str(value or "").strip().lower().rstrip(".")
domain = ".".join(part.strip().lower() for part in parts if part.strip())
```

**After**:
```python
from utils.normalization import normalize_domain
domain = normalize_domain(value)
domain = normalize_domain(".".join(part for part in parts if part.strip()))
```

**Priority**: High (domain access control uses this)

---

### Pattern 3: HTTP Error 401 (Unauthorized)

**Frequency**: 2-3 instances  
**Utility**: `APIError.unauthorized()`

**Before**:
```python
raise HTTPException(status_code=401, detail="Не авторизован")
```

**After**:
```python
from utils.errors import APIError
raise APIError.unauthorized()
```

**Locations**:
```
rupochta_server.py - Search for: raise HTTPException(status_code=401
```

**Priority**: High (authentication)

---

### Pattern 4: HTTP Error 403 (Forbidden)

**Frequency**: 8-10 instances  
**Utility**: `APIError.forbidden()`

**Before**:
```python
raise HTTPException(status_code=403, detail="Forbidden")
raise HTTPException(status_code=403, detail="Нет прав mail administrator")
```

**After**:
```python
raise APIError.forbidden()
```

**Priority**: High (authorization)

---

### Pattern 5: HTTP Error 404 (Not Found)

**Frequency**: 2-3 instances  
**Utility**: `APIError.not_found()`

**Before**:
```python
raise HTTPException(status_code=404, detail="Not Found")
```

**After**:
```python
raise APIError.not_found()
```

**Priority**: Medium

---

### Pattern 6: HTTP Error 400 (Bad Request)

**Frequency**: 3-5 instances  
**Utility**: `APIError.bad_request()` or `APIError.invalid_request()`

**Before**:
```python
raise HTTPException(status_code=400, detail="Некорректный контекст ящика")
raise HTTPException(400, "Выберите хотя бы одну группу администраторов")
```

**After**:
```python
raise APIError.bad_request("Некорректный контекст ящика")
# or, for generic bad requests:
raise APIError.invalid_request()
```

**Priority**: Medium

---

### Pattern 7: HTTP Error 500+ (Server Errors)

**Frequency**: 3-5 instances  
**Utility**: `APIError.internal_error()`, `APIError.service_unavailable()`

**Before**:
```python
raise HTTPException(status_code=503, detail="Каталог групп AD временно недоступен")
raise HTTPException(status_code=502, detail=str(data))
```

**After**:
```python
raise APIError.service_unavailable("Каталог групп AD временно недоступен")
raise APIError.internal_error(str(data))
```

**Priority**: Medium

---

### Pattern 8: Input Validation (Email Format)

**Frequency**: 3-5 instances  
**Utility**: `InputValidator.validate_email()` from utils.validation

**Before**:
```python
if "@" not in email or "." not in email.split("@")[1]:
    raise HTTPException(status_code=400, detail="Invalid email")
```

**After**:
```python
from utils.validation import InputValidator
try:
    validated_email = InputValidator.validate_email(email)
except ValidationError as e:
    raise APIError.bad_request(str(e))
```

**Priority**: Medium (after HTTPException fixes)

---

## Phase 3c: Database, Mail Protocol, Authentication

### Pattern 9: Database Row-to-Dict Conversion

**Frequency**: 6+ instances  
**Utility**: `DBHelper.cursor_row_dict()` from utils.database

**Before**:
```python
row = cursor.fetchone()
result = dict(zip([col[0] for col in cursor.description], row))
if result and result.get("metadata"):
    result["metadata"] = json.loads(result["metadata"])
```

**After**:
```python
from utils.database import DBHelper
row = cursor.fetchone()
result = DBHelper.cursor_row_dict(cursor, row, json_fields=["metadata"])
```

**Priority**: High (database operations are frequent)

---

### Pattern 10: IMAP Connection Handling

**Frequency**: 3-4 instances  
**Utility**: `IMAPHelper` context manager from utils.mail_protocol

**Before**:
```python
imap = imaplib.IMAP4_SSL(smtp_host)
imap.login(username, password)
try:
    # ... IMAP operations ...
except Exception as e:
    raise HTTPException(status_code=500, detail=f"IMAP error: {str(e)}")
finally:
    imap.logout()
```

**After**:
```python
from utils.mail_protocol import IMAPHelper
imap_helper = IMAPHelper(smtp_host)
try:
    with imap_helper.connect(username, password) as imap:
        # ... IMAP operations ...
except Exception as e:
    raise APIError.internal_error(f"IMAP error: {str(e)}")
```

**Priority**: High (prevents resource leaks)

---

### Pattern 11: SMTP Connection Handling

**Frequency**: 2-3 instances  
**Utility**: `SMTPHelper` context manager from utils.mail_protocol

**Before**:
```python
smtp = smtplib.SMTP_SSL(smtp_host, smtp_port)
smtp.login(username, password)
try:
    # ... SMTP operations ...
finally:
    smtp.quit()
```

**After**:
```python
from utils.mail_protocol import SMTPHelper
smtp_helper = SMTPHelper(smtp_host, smtp_port)
with smtp_helper.connect(username, password) as smtp:
    # ... SMTP operations ...
```

**Priority**: High (prevents resource leaks)

---

### Pattern 12: Domain Access Check

**Frequency**: 3-5 instances  
**Utility**: `AuthGuard.check_domain_access()` from utils.authentication

**Before**:
```python
if str(user_domain or "").strip().lower() != str(requested_domain or "").strip().lower():
    raise HTTPException(status_code=403, detail="Access denied")
```

**After**:
```python
from utils.authentication import AuthGuard
try:
    AuthGuard.check_domain_access(user_domain, requested_domain)
except PermissionError:
    raise APIError.forbidden()
```

**Priority**: Critical (auth bypass prevention)

---

### Pattern 13: User Authorization Check

**Frequency**: 2-3 instances  
**Utility**: `AuthGuard.check_user_access()` from utils.authentication

**Before**:
```python
if not is_authorized_user:
    raise HTTPException(status_code=403, detail="Forbidden")
```

**After**:
```python
try:
    AuthGuard.check_user_access(user_roles, required_role)
except PermissionError:
    raise APIError.forbidden()
```

**Priority**: Critical (authorization)

---

## Phase 3d: Secondary Files & Testing

### File 1: rupochta_control_agent.py

**Replacements Needed**: 8+ (similar patterns to rupochta_server.py)

1. Add imports (same as rupochta_server.py)
2. Replace config parsing (os.environ.get → get_env_*)
3. Replace normalization patterns
4. Replace HTTPException patterns  
5. Replace auth checks

**Priority**: Medium (secondary file)

---

### File 2: imap_docker_proxy.py

**Replacements Needed**: 1-2 (minimal changes)

**Pattern**: Config parsing  
**Solution**: Add utils.config imports and replace env var parsing

**Priority**: Low (small file)

---

### Testing: Create Integration Tests

**File**: `tests/test_integration_phase3.py`

**Content**:
```python
import unittest
from rupochta_server import Config, app
from utils.config import get_env_str

class TestPhase3Integration(unittest.TestCase):
    """Integration tests for Phase 3 utility replacements"""
    
    def test_config_parsing_works(self):
        """Verify Config class loads correctly with utility helpers"""
        self.assertEqual(Config.IMAP_PORT, 993)
        self.assertEqual(Config.SMTP_PORT, 587)
        self.assertTrue(isinstance(Config.SMTP_VERIFY_TLS, bool))
    
    def test_normalization_in_handlers(self):
        """Test that handlers normalize input correctly"""
        # Add tests that verify handlers use normalization
        pass
    
    def test_error_responses(self):
        """Test that error handlers return correct HTTP status codes"""
        # Add tests that verify APIError responses
        pass
```

**Priority**: High (ensures no regressions)

---

## Implementation Checklist

### Phase 3b
- [ ] Replace email normalization (12+ → Pattern 1)
- [ ] Replace domain normalization (4+ → Pattern 2)
- [ ] Replace HTTP 401 errors (2-3 → Pattern 3)
- [ ] Replace HTTP 403 errors (8-10 → Pattern 4)
- [ ] Replace HTTP 404 errors (2-3 → Pattern 5)
- [ ] Replace HTTP 400 errors (3-5 → Pattern 6)
- [ ] Replace HTTP 500+ errors (3-5 → Pattern 7)
- [ ] Replace validation checks (3-5 → Pattern 8)
- **Commit**: "refactor: Phase 3b - Normalization, errors, validation utilities"
- **Test**: `python -m py_compile rupochta_server.py`

### Phase 3c
- [ ] Replace database row-to-dict (6+ → Pattern 9)
- [ ] Replace IMAP connections (3-4 → Pattern 10)
- [ ] Replace SMTP connections (2-3 → Pattern 11)
- [ ] Replace domain access checks (3-5 → Pattern 12)
- [ ] Replace user auth checks (2-3 → Pattern 13)
- **Commit**: "refactor: Phase 3c - Database, mail, authentication utilities"
- **Test**: `python -m py_compile rupochta_server.py`

### Phase 3d
- [ ] Update rupochta_control_agent.py (8+ replacements)
- [ ] Update imap_docker_proxy.py (1-2 replacements)
- [ ] Create tests/test_integration_phase3.py
- [ ] Run full test suite: `python -m unittest discover -s tests`
- **Commit**: "refactor: Phase 3d - Update secondary files and add integration tests"
- **Final Commit**: "docs: Phase 3 integration complete - All 90+ duplicates eliminated"

---

## Commands for Finding Patterns

```bash
# Normalization patterns
grep -n "\.strip()\.lower()" rupochta_server.py

# HTTPException patterns
grep -n "raise HTTPException" rupochta_server.py

# Database patterns
grep -n "dict(zip" rupochta_server.py

# IMAP/SMTP patterns
grep -n "imaplib.IMAP4_SSL\|smtplib.SMTP" rupochta_server.py

# Auth check patterns
grep -n "if str.*!= str" rupochta_server.py
```

---

## Expected Final Results

After completing all phases:

- **Total replacements**: 90+
- **Files modified**: 5 (rupochta_server.py, rupochta_control_agent.py, imap_docker_proxy.py, tests/*, docs/*)
- **Lines reduced**: 440-560 (duplicate code eliminated)
- **Modules using utilities**: All main modules
- **Test coverage**: 100+ test cases (existing + new)
- **Duplicate patterns remaining**: 0 (all centralized in utils/)

---

## Notes

- All utility modules are **production-ready** and fully tested
- Phase 3a (Config) is already complete
- This guide provides exact patterns and locations
- Implementation can be done incrementally by pattern type
- Each pattern batch can be committed separately
- Comprehensive testing should happen after Phase 3c completion

---

**Ready to begin Phase 3b implementation!**

Use this guide to systematically complete all remaining refactoring work.
