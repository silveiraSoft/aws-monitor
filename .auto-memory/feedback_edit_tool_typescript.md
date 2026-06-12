---
name: feedback-edit-tool-typescript
description: Edit tool truncates TypeScript CDK files — always use Python scripts for edits in this project
metadata:
  type: feedback
---

Never use the Edit tool on TypeScript (.ts) files in this project, especially monitor-agent-stack.ts and chat-frontend-stack.ts.

**Why:** The Edit tool truncates the file at the replacement point when old_string or new_string contains double quotes inside single-quoted TypeScript strings (e.g. `exportName: 'AwsMonitorAgentAliasId'`). This leaves the file syntactically broken and causes tsc errors. Happens regardless of OneDrive vs local folder.

**How to apply:** For any change to .ts files, use a Python script:
```python
with open('lib/some-stack.ts', 'r') as f:
    content = f.read()
content = content.replace(OLD, NEW)
with open('lib/some-stack.ts', 'w') as f:
    f.write(content)
```

After any file edit, always verify:
```bash
wc -l file.ts && tail -5 file.ts && npx tsc --noEmit
```

If truncated: restore with Python using `git show HEAD:path/to/file` as source for the missing ending.

**Also applies to:** chat-frontend-stack.ts (has embedded Python template literals inside TypeScript template strings — especially prone to truncation).
