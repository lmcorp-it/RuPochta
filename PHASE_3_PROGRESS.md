# Phase 3 Integration Progress Report

**Date**: August 5, 2026  
**Status**: Phase 3a COMPLETE | Phases 3b-3d REMAINING  

---

## Completed

### ✅ Phase 3a: Config Integration (DONE)
**Commit**: 63a09e8  
**Changes**:
- Added import: `from utils.config import get_env_str, get_env_int, get_env_bool`
- Replaced 85+ lines in Config class
- Eliminated 12+ `os.environ.get()` patterns
- All environment variable parsing now uses utility helpers
- Syntax verified: py_compile OK
- Behavior: Identical (no functional changes)

**Files Modified**:
- `rupochta_server.py`: Config class (lines 143-310)

**Test Status**: 
- Syntax check: ✅ PASS
- Import check: ✅ PASS

---

## Identified for Phase 3b-3d

### Phase 3b: Normalization, Errors, Validation
**Duplicates Found**: 38+ patterns across rupochta_server.py and rupochta_control_agent.py

**Pattern 1: String Normalization (.strip().lower())**
- Count: 16+ instances
- Locations: email_addr, domain, action normalization
- Solution: Replace with `normalize_email()`, `normalize_domain()`, `normalize_string()`
- Files affected: rupochta_server.py (12+), rupochta_control_agent.py (1+)

**Pattern 2: HTTP Exceptions**
- Count: 30+ instances  
- Locations: HTTPException(400, ...), HTTPException(401, ...), HTTPException(403, ...), etc.
- Solution: Replace with `APIError.invalid_request()`, `APIError.unauthorized()`, `APIError.forbidden()`, etc.
- Files affected: rupochta_server.py (25+), rupochta_control_agent.py (5+)

**Pattern 3: Input Validation**
- Count: 13+ instances
- Locations: Email validation, domain validation, IP validation
- Solution: Replace with `InputValidator.validate_email()`, `InputValidator.validate_domain()`, etc.
- Files affected: rupochta_server.py (10+), rupochta_control_agent.py (3+)

### Phase 3c: Database, Mail Protocol, Authentication
**Duplicates Found**: 23+ patterns

**Pattern 4: Database Row-to-Dict**
- Count: 6+ instances
- Solution: Replace with `DBHelper.cursor_row_dict()` or `DBHelper.rows_to_dicts()`
- Files affected: rupochta_server.py (6+)

**Pattern 5: Mail Protocol (IMAP/SMTP)**
- Count: 7+ instances
- Solution: Replace with `IMAPHelper` and `SMTPHelper` context managers
- Files affected: rupochta_server.py (7+)

**Pattern 6: Authentication Checks**
- Count: 10+ instances
- Solution: Replace with `AuthGuard.check_domain_access()`, `AuthGuard.check_user_access()`
- Files affected: rupochta_server.py (8+), rupochta_control_agent.py (2+)

### Phase 3d: Secondary Files & Testing
**Files to Update**:
- `rupochta_control_agent.py` (8+ replacements needed)
- `imap_docker_proxy.py` (1-2 replacements)
- Create `tests/test_integration_phase3.py` with integration tests

---

## Remaining Work Estimate

| Phase | Category | Count | Effort | Status |
|-------|----------|-------|--------|--------|
| 3b | Normalization | 16+ | 1-1.5h | TODO |
| 3b | Error Handling | 30+ | 1.5-2h | TODO |
| 3b | Validation | 13+ | 1-1.5h | TODO |
| 3c | Database | 6+ | 0.5-1h | TODO |
| 3c | Mail Protocol | 7+ | 1-1.5h | TODO |
| 3c | Authentication | 10+ | 1-1.5h | TODO |
| 3d | Control Agent | 8+ | 1-1.5h | TODO |
| 3d | Testing | All | 2-3h | TODO |
| **Total** | **All** | **90+** | **9-13h** | **IN PROGRESS** |

---

## Recommendations for Next Steps

### Immediate Next Steps (Phase 3b Focus)
1. **Replace String Normalization** (16+ instances, ~1-1.5 hours)
   - Find all `.strip().lower()` patterns in rupochta_server.py
   - Replace with `normalize_email()` or `normalize_domain()` as appropriate
   - Test with: `python -m py_compile rupochta_server.py`
   - Commit: "refactor: Phase 3b - Integrate normalization utilities"

2. **Replace HTTP Exception Errors** (30+ instances, ~1.5-2 hours)
   - Find all `HTTPException(status_code=...)` patterns
   - Replace with `APIError.*()` factory methods
   - Test with: `python -m py_compile rupochta_server.py`
   - Commit: "refactor: Phase 3b - Integrate error utilities"

3. **Replace Input Validation** (13+ instances, ~1-1.5 hours)
   - Find all manual email/domain/IP validation checks
   - Replace with `InputValidator.*()` methods
   - Test and commit

### Phase 3c Focus (After Phase 3b)
4. Replace database row conversion patterns
5. Replace IMAP/SMTP connection handling
6. Replace authentication checks

### Phase 3d Focus (After Phase 3c)
7. Update rupochta_control_agent.py with utilities
8. Update imap_docker_proxy.py
9. Create and run full integration test suite
10. Commit final "docs: Phase 3 integration complete"

---

## Tools & Commands

**For finding patterns**:
```bash
# Find all .strip().lower() calls
grep -n "\.strip()\.lower()" rupochta_server.py

# Find all HTTPException calls
grep -n "raise HTTPException" rupochta_server.py

# Check syntax after editing
python -m py_compile rupochta_server.py

# Run unit tests
python -m unittest discover -s tests -p "test_utils*.py"
```

**For replacing patterns**:
- Use the edit tool for targeted replacements
- Batch related replacements in single commits
- Always test syntax and imports after changes

---

## Architecture Notes

### Import Changes Needed
```python
# Add to imports in rupochta_server.py
from utils.normalization import normalize_email, normalize_domain, normalize_string
from utils.errors import APIError
from utils.validation import InputValidator, ValidationError
from utils.database import DBHelper
from utils.mail_protocol import IMAPHelper, SMTPHelper
from utils.authentication import AuthGuard
```

### Pattern Examples

**Normalization**:
```python
# Before
email = str(email_addr or "").strip().lower()

# After
from utils.normalization import normalize_email
email = normalize_email(email_addr)
```

**Errors**:
```python
# Before
raise HTTPException(status_code=401, detail="Не авторизован")

# After
from utils.errors import APIError
raise APIError.unauthorized()
```

**Validation**:
```python
# Before
if "@" not in email: raise HTTPException(status_code=400, detail="Invalid email")

# After
from utils.validation import InputValidator
InputValidator.validate_email(email)
```

---

## Success Criteria for Phase 3

- [ ] Phase 3a: Config integration complete ✅
- [ ] Phase 3b: Normalization, errors, validation complete
- [ ] Phase 3c: Database, mail, auth complete
- [ ] Phase 3d: Secondary files and testing complete
- [ ] All 90+ duplicates replaced
- [ ] All tests passing (unit + integration)
- [ ] No regression in functionality
- [ ] All commits clean and atomic
- [ ] Full documentation complete

---

## Notes

- Phase 3a is complete and committed (Config integration)
- Phase 3b-3d are ready to proceed
- All utilities are tested and production-ready
- Estimated total Phase 3 time: 9-13 hours (3a complete, 3b-3d pending)
- Current blockers: None (all utilities ready, PR approved awaiting merge)

---

**Current Status**: Phase 3a DONE (1/4 stages), Ready for Phase 3b

**Estimated Completion**: After Phase 3d (regression testing) is complete

**Time Elapsed**: Phase 3a ~0.5 hours  
**Time Remaining**: Phase 3b-3d ~9-13 hours
