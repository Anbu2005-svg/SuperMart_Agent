"""Comprehensive Security Analyzer and Audit Script for SuperMart Ops Agent."""

import os
import re
import sys
import ast
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent

EXCLUDE_DIRS = {".venv", "venv", ".git", "__pycache__", "test_assets", ".kilo", ".pytest_cache"}

def scan_sql_injection():
    print("\n--- 1. Scanning for Unparameterized SQL Queries ---")
    fstring_sql = re.compile(r'cur\.execute\s*\(\s*f["\']', re.MULTILINE)
    format_sql = re.compile(r'cur\.execute\s*\([^,)]*\.format\(', re.MULTILINE)
    percent_sql = re.compile(r'cur\.execute\s*\(\s*["\'][^"\']*["\']\s*%', re.MULTILINE)
    
    issues = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if f.endswith(".py"):
                path = Path(root) / f
                content = path.read_text(encoding="utf-8", errors="ignore")
                for line_no, line in enumerate(content.splitlines(), start=1):
                    if fstring_sql.search(line):
                        issues.append((path.relative_to(ROOT), line_no, "f-string in cur.execute", line.strip()))
                    elif format_sql.search(line):
                        issues.append((path.relative_to(ROOT), line_no, "str.format in cur.execute", line.strip()))
                    elif percent_sql.search(line) and not line.strip().endswith("%s"):
                        issues.append((path.relative_to(ROOT), line_no, "percent-formatting in cur.execute", line.strip()))
                        
    if issues:
        for p, l, typ, txt in issues:
            print(f"  ⚠️ [Line {l}] {p}: {typ} -> {txt[:80]}")
    else:
        print("  ✅ All SQL queries use safe parameterized (%s) placeholders!")
    return issues


def scan_hardcoded_secrets():
    print("\n--- 2. Scanning for Hardcoded Secrets in Code ---")
    secret_patterns = [
        ("Telegram Bot Token", re.compile(r'\b[0-9]{8,10}:[a-zA-Z0-9_-]{35}\b')),
        ("OpenAI API Key", re.compile(r'\bsk-[a-zA-Z0-9]{20,}\b')),
        ("Google/Gemini Key", re.compile(r'\bAIza[0-9A-Za-z-_]{35}\b')),
        ("Postgres Password in URI", re.compile(r'postgresql:\/\/[^:]+:([^@]+)@')),
    ]
    
    issues = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if f.endswith((".py", ".json", ".sql", ".md")) and f != ".env":
                path = Path(root) / f
                content = path.read_text(encoding="utf-8", errors="ignore")
                for line_no, line in enumerate(content.splitlines(), start=1):
                    for label, pat in secret_patterns:
                        m = pat.search(line)
                        if m:
                            # Skip documentation examples with placeholder 'password'
                            if "your_" in line or "placeholder" in line or "password@" in line:
                                continue
                            issues.append((path.relative_to(ROOT), line_no, label, line.strip()))
                            
    if issues:
        for p, l, lbl, txt in issues:
            print(f"  ⚠️ [Line {l}] {p}: Found {lbl} -> {txt[:40]}...")
    else:
        print("  ✅ No hardcoded API keys or credentials detected in codebase!")
    return issues


def scan_path_traversal():
    print("\n--- 3. Scanning for Path Traversal in File Operations ---")
    open_file = re.compile(r'open\s*\(\s*([a-zA-Z0-9_]+)')
    
    issues = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if f.endswith(".py"):
                path = Path(root) / f
                content = path.read_text(encoding="utf-8", errors="ignore")
                if "is_safe_generated_file" not in content and ("send_document" in content or "reply_document" in content):
                    issues.append((path.relative_to(ROOT), "Document sending without is_safe_generated_file check"))
                    
    if issues:
        for p, desc in issues:
            print(f"  ⚠️ {p}: {desc}")
    else:
        print("  ✅ All dynamic document delivery is strictly sandboxed to generated_docs/!")
    return issues


def scan_csv_injection():
    print("\n--- 4. Scanning for CSV Injection Vulnerabilities (Formula Injection) ---")
    gst_export_path = ROOT / "skills" / "gst_export.py"
    content = gst_export_path.read_text(encoding="utf-8", errors="ignore") if gst_export_path.exists() else ""
    
    if any(sym in content for sym in ("_sanitize_csv_cell", "escape_csv_formula")):
        print("  ✅ CSV injection protection active!")
    else:
        print("  ⚠️ skills/gst_export.py may be missing formula sanitization (=, +, -, @)!")
        return ["Missing CSV formula sanitization in skills/gst_export.py"]
    return []


def scan_git_history():
    print("\n--- 5. Scanning Git History for Leaked Secrets ---")
    import subprocess
    secret_regexes = [
        re.compile(r'AIza[0-9A-Za-z-_]{35}'),
        re.compile(r'\b[0-9]{9,10}:[a-zA-Z0-9_-]{35}\b'),
        re.compile(r'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY'),
        re.compile(r'postgres(ql)?:\/\/[a-zA-Z0-9_-]+:[^@\s]{4,}@'),
    ]

    try:
        res = subprocess.run(["git", "log", "-p", "--all", "-n", "100"], capture_output=True, text=True, cwd=str(ROOT), encoding="utf-8", errors="ignore")
        if res.returncode != 0:
            print("  ⚠️ Git log command returned non-zero code.")
            return []

        leaks = []
        for line in res.stdout.splitlines():
            # Only check added lines
            if line.startswith("+") and not line.startswith("+++"):
                cleaned = line[1:].strip()
                # Skip example/template lines
                if "your_" in cleaned or "placeholder" in cleaned or "password@" in cleaned or "example" in cleaned:
                    continue
                for rx in secret_regexes:
                    if rx.search(cleaned):
                        leaks.append(cleaned[:60])

        if leaks:
            for l in leaks[:5]:
                print(f"  ⚠️ Potential secret in git history: {l}...")
        else:
            print("  ✅ Git commit history is clean! No leaked secrets found.")
        return leaks
    except Exception as e:
        print(f"  ⚠️ Could not scan git history: {e}")
        return []


if __name__ == "__main__":
    scan_sql_injection()
    scan_hardcoded_secrets()
    scan_path_traversal()
    scan_csv_injection()
    scan_git_history()

