#!/usr/bin/env python3
"""Exercise native approval gates with the deployed operator policy, offline."""
from pathlib import Path
from unittest.mock import patch

import yaml
from tools import approval, approval_context

config = yaml.safe_load(Path('/opt/hermes/qqbot-hk/hermes-config.yaml').read_text())
assert config.get('approvals', {}).get('mode') == 'off'
command = "cd /opt/data/cache/scratch && python3 - <<'EOF'\nprint('synthetic')\nEOF"
assert approval.detect_dangerous_command(command)[0], 'fixture must reach the native approval gate'

with patch('hermes_cli.config.load_config_readonly', return_value=config), \
        patch.object(approval, '_human_decision', side_effect=AssertionError('unexpected approval wait')), \
        patch('agent.auxiliary_client.call_llm', side_effect=AssertionError('unexpected guardian request')):
    assert approval_context._get_approval_mode() == 'off'
    assert approval.check_dangerous_command(command, 'local')['approved'] is True
    assert approval.request_tool_approval('terminal', 'synthetic approval rule')['approved'] is True

print('QQ_TOOL_APPROVALS=off HEREDOC=allowed HUMAN_WAIT=absent GUARDIAN_REQUEST=absent')
