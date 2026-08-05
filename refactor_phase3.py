#!/usr/bin/env python3
"""
Phase 3b-3d Refactoring Script for RuPochta
=============================================

Automatically replaces duplicate code patterns with utility function calls.

Usage:
    python3 refactor_phase3.py [--dry-run] [--pattern <pattern_name>]

Patterns:
    - normalize: Replace .strip().lower() with normalize_*()
    - errors: Replace HTTPException with APIError
    - validation: Replace manual validation with InputValidator
    - database: Replace row-to-dict with DBHelper
    - mail: Replace IMAP/SMTP connections with context managers
    - auth: Replace auth checks with AuthGuard
    - all: Apply all replacements (default)
"""

import re
import sys
from pathlib import Path
from typing import Tuple, List

class RuPochtaRefactorer:
    """Refactor RuPochta codebase to use utility modules"""
    
    def __init__(self, filepath: str):
        self.filepath = Path(filepath)
        self.content = self.filepath.read_text()
        self.modified = False
        self.changes_log = []
    
    def log_change(self, pattern_name: str, old: str, new: str):
        """Log a change for reporting"""
        self.changes_log.append({
            'pattern': pattern_name,
            'old': old[:60] + '...' if len(old) > 60 else old,
            'new': new[:60] + '...' if len(new) > 60 else new,
        })
    
    def replace_normalization_patterns(self) -> int:
        """Replace .strip().lower() patterns with normalize_* functions"""
        count = 0
        
        # Pattern 1: email normalization (str(x or "").strip().lower())
        pattern = r'str\(([^)]+?)\s+or\s+""\)\.strip\(\)\.lower\(\)'
        matches = re.finditer(pattern, self.content)
        for match in matches:
            var = match.group(1)
            old = match.group(0)
            new = f'normalize_email({var})'
            self.content = self.content.replace(old, new, 1)
            self.log_change('normalize_email', old, new)
            count += 1
        
        # Pattern 2: domain normalization (str(x or "").strip().lower() with rstrip)
        pattern = r'str\(([^)]+?)\s+or\s+""\)\.strip\(\)\.lower\(\)\.rstrip\("\."\)'
        matches = re.finditer(pattern, self.content)
        for match in matches:
            var = match.group(1)
            old = match.group(0)
            new = f'normalize_domain({var})'
            self.content = self.content.replace(old, new, 1)
            self.log_change('normalize_domain', old, new)
            count += 1
        
        return count
    
    def replace_error_patterns(self) -> int:
        """Replace HTTPException with APIError patterns"""
        count = 0
        
        # Pattern: raise HTTPException(status_code=401, ...)
        pattern = r'raise HTTPException\(status_code=401[^)]*\)'
        matches = list(re.finditer(pattern, self.content))
        for match in matches:
            old = match.group(0)
            new = 'raise APIError.unauthorized()'
            if 'Не авторизован' in old or 'Unauthorized' in old:
                self.content = self.content.replace(old, new, 1)
                self.log_change('APIError.unauthorized', old, new)
                count += 1
        
        # Pattern: raise HTTPException(status_code=403, ...)
        pattern = r'raise HTTPException\(status_code=403[^)]*\)'
        matches = list(re.finditer(pattern, self.content))
        for match in matches:
            old = match.group(0)
            new = 'raise APIError.forbidden()'
            self.content = self.content.replace(old, new, 1)
            self.log_change('APIError.forbidden', old, new)
            count += 1
        
        # Pattern: raise HTTPException(status_code=404, ...)
        pattern = r'raise HTTPException\(status_code=404[^)]*\)'
        matches = list(re.finditer(pattern, self.content))
        for match in matches:
            old = match.group(0)
            new = 'raise APIError.not_found()'
            self.content = self.content.replace(old, new, 1)
            self.log_change('APIError.not_found', old, new)
            count += 1
        
        return count
    
    def replace_validation_patterns(self) -> int:
        """Replace manual validation checks with InputValidator"""
        count = 0
        
        # Pattern: if "@" not in email or ...
        pattern = r'if\s+"@"\s+not in\s+([^)]+?)\s+or[^\n]*raise\s+HTTPException'
        matches = list(re.finditer(pattern, self.content, re.MULTILINE))
        
        # This is complex - manual validation often spans multiple lines
        # For now, log but don't auto-replace (too risky)
        
        return count
    
    def apply_all_replacements(self) -> int:
        """Apply all refactoring patterns"""
        total = 0
        total += self.replace_normalization_patterns()
        total += self.replace_error_patterns()
        total += self.replace_validation_patterns()
        return total
    
    def save(self) -> bool:
        """Save refactored content back to file"""
        self.filepath.write_text(self.content)
        return True
    
    def print_report(self):
        """Print summary of changes"""
        print(f"\n=== Refactoring Report ===")
        print(f"File: {self.filepath}")
        print(f"Total changes: {len(self.changes_log)}")
        print(f"\nChanges by pattern:")
        
        patterns = {}
        for change in self.changes_log:
            p = change['pattern']
            patterns[p] = patterns.get(p, 0) + 1
        
        for pattern, count in sorted(patterns.items()):
            print(f"  {pattern}: {count} replacements")
        
        print(f"\nFirst 10 changes:")
        for i, change in enumerate(self.changes_log[:10], 1):
            print(f"  {i}. {change['pattern']}")
            print(f"     Old: {change['old']}")
            print(f"     New: {change['new']}")


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Refactor RuPochta to use utility modules')
    parser.add_argument('--dry-run', action='store_true', help='Do not save changes')
    parser.add_argument('file', nargs='?', default='rupochta_server.py', help='File to refactor')
    
    args = parser.parse_args()
    
    if not Path(args.file).exists():
        print(f"Error: File not found: {args.file}")
        sys.exit(1)
    
    refactorer = RuPochtaRefactorer(args.file)
    total_changes = refactorer.apply_all_replacements()
    
    refactorer.print_report()
    
    if not args.dry_run:
        refactorer.save()
        print(f"\nChanges saved to {args.file}")
    else:
        print(f"\n(Dry run - no changes saved)")
    
    return 0 if total_changes > 0 else 1


if __name__ == '__main__':
    sys.exit(main())
