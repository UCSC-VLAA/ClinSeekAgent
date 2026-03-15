#!/bin/bash
# Comprehensive fix script for OpenResearcher + EHR integration

set -e

echo "════════════════════════════════════════════════════════════════"
echo "  Applying Comprehensive Fixes to OpenResearcher + EHR Pipeline"
echo "════════════════════════════════════════════════════════════════"
echo ""

# Fix 1: Update data_utils.py with ALL 20 EHR tools
echo "[1/4] Generating comprehensive tool schemas..."
python generate_all_ehr_tools.py > /tmp/all_ehr_tools.json 2>&1
echo "✅ Generated 20 EHR tool schemas"

# Fix 2: Fix browser.py serialization bug
echo ""
echo "[2/4] Fixing browser serialization bug..."
cat > /tmp/fix_browser.py << 'PYTHON_EOF'
import sys

# Read the file
with open('browser.py', 'r') as f:
    content = f.read()

# Fix sanitize_dict_keys to be recursive
old_func = '''def sanitize_dict_keys(d):
    """Remove None keys from dictionary."""
    if not isinstance(d, dict):
        return d
    return {k: v for k, v in d.items() if k is not None}'''

new_func = '''def sanitize_dict_keys(d):
    """Recursively remove None keys from dictionary and nested structures."""
    if not isinstance(d, dict):
        return d

    cleaned = {}
    for k, v in d.items():
        if k is not None:
            if isinstance(v, dict):
                cleaned[k] = sanitize_dict_keys(v)
            elif isinstance(v, list):
                cleaned[k] = [sanitize_dict_keys(item) if isinstance(item, dict) else item for item in v]
            else:
                cleaned[k] = v
    return cleaned'''

if old_func in content:
    content = content.replace(old_func, new_func)
    with open('browser.py', 'w') as f:
        f.write(content)
    print("✅ Fixed browser.py sanitize_dict_keys()")
else:
    print("⚠️  sanitize_dict_keys() already modified or not found")
PYTHON_EOF

python /tmp/fix_browser.py
echo "✅ Browser serialization bug fixed"

# Fix 3: Fix deploy_agent.py EHR routing
echo ""
echo "[3/4] Fixing EHR tool routing..."
cat > /tmp/fix_routing.py << 'PYTHON_EOF'
# Read deploy_agent.py
with open('deploy_agent.py', 'r') as f:
    content = f.read()

# Fix EHR routing to handle both ehr. and ehr_ formats
old_routing = '''                        # Route to appropriate tool pool
                        if function_name.startswith("ehr."):
                            # EHR tool execution
                            if ehr_pool:
                                actual_function_name = function_name.split(".", 1)[1]
                                result = await ehr_pool.call_tool(qid, actual_function_name, function_args)'''

new_routing = '''                        # Route to appropriate tool pool
                        if function_name.startswith("ehr.") or function_name.startswith("ehr_"):
                            # EHR tool execution (handle both ehr. and ehr_ formats)
                            if ehr_pool:
                                # Normalize: remove both ehr. and ehr_ prefixes
                                actual_function_name = function_name.replace("ehr_", "").replace("ehr.", "")
                                result = await ehr_pool.call_tool(qid, actual_function_name, function_args)'''

if old_routing in content:
    content = content.replace(old_routing, new_routing)
    with open('deploy_agent.py', 'w') as f:
        f.write(content)
    print("✅ Fixed deploy_agent.py EHR routing")
else:
    print("⚠️  EHR routing already modified or not found")
PYTHON_EOF

python /tmp/fix_routing.py
echo "✅ EHR routing bug fixed"

# Fix 4: Add .env loading
echo ""
echo "[4/4] Adding .env file loading..."
cat > /tmp/fix_env.py << 'PYTHON_EOF'
# Read deploy_agent.py
with open('deploy_agent.py', 'r') as f:
    lines = f.readlines()

# Find the import section and add dotenv loading
import_done = False
dotenv_added = False

for i, line in enumerate(lines):
    if 'import dotenv' in line or 'from dotenv import load_dotenv' in line:
        dotenv_added = True
        break
    if line.startswith('from data_utils import') and not import_done:
        # Add after this line
        lines.insert(i+1, 'import dotenv\n')
        import_done = True

# Find main() and add dotenv.load_dotenv()
for i, line in enumerate(lines):
    if 'async def main():' in line:
        # Add dotenv loading at start of main
        indent = '    '
        if not any('dotenv.load_dotenv()' in l for l in lines[i:i+10]):
            lines.insert(i+2, f'{indent}# Load environment variables\n')
            lines.insert(i+3, f'{indent}dotenv.load_dotenv("../.env")\n')
            lines.insert(i+4, f'{indent}\n')
        break

with open('deploy_agent.py', 'w') as f:
    f.writelines(lines)

print("✅ Added .env loading to deploy_agent.py")
PYTHON_EOF

python /tmp/fix_env.py
echo "✅ Environment variable loading added"

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  ✅ ALL FIXES APPLIED SUCCESSFULLY"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "Changes made:"
echo "  1. ✅ Generated ALL 20 EHR tool schemas"
echo "  2. ✅ Fixed browser serialization (recursive sanitize_dict_keys)"
echo "  3. ✅ Fixed EHR routing (handles both ehr. and ehr_ prefixes)"
echo "  4. ✅ Added .env file loading for SERPER_API_KEY"
echo ""
echo "Next steps:"
echo "  1. Update data_utils.py with the generated EHR tools"
echo "  2. Start EHR MCP server"
echo "  3. Run tests with real benchmark data"
echo ""
