"""Use the AstrBot environment's Python; no provider calls."""
import logging
from types import SimpleNamespace

from patcher import DEFAULTS, REQUIRED, TOKEN, Patches


def main():
    from astrbot.core import astr_main_agent as agent
    from astrbot.core.skills import skill_manager

    cfg = {}
    patches = Patches(cfg, logging.getLogger(__name__))
    original_fn = agent._build_local_mode_prompt
    original_code = original_fn.__code__
    original = original_fn()
    for key, (_, text) in DEFAULTS.items():
        args = {k: 'VALUE_{literal}' for k in REQUIRED.get(key, ())}
        assert set(TOKEN.findall(text)) == args.keys(), key
        output = patches.render(key, 'UNEXPECTED', **args)
        assert output != 'UNEXPECTED', key
        if args:
            assert output.count('VALUE_{literal}') == len(args), key
    patches.install()
    try:
        assert len(patches.saved) == 12
        assert original_fn is agent._build_local_mode_prompt
        assert '当前操作系统' in original_fn()
        skills = [SimpleNamespace(name='sample', description='When using tools: leave this unchanged.', source_type='plugin', path='/tmp/sample/SKILL.md')]
        output = skill_manager.build_skills_prompt(skills)
        assert '可用技能' in output and skills[0].description in output and skills[0].path in output
        existing = 'Third-party text: ' + agent.LLM_SAFETY_MODE_SYSTEM_PROMPT
        request = SimpleNamespace(system_prompt=existing)
        agent._apply_llm_safety_mode(SimpleNamespace(safety_mode_strategy='system_prompt'), request)
        assert request.system_prompt == DEFAULTS['safety'][1] + '\n\n' + existing
        cfg['local'] = '自定义 {os} / {shell}'
        assert original_fn().startswith('自定义 ')
        cfg['local'] = '@original'
        assert original_fn().startswith('Local computer tools')  # shell remains separately translated
        cfg['enabled'] = False
        assert original_fn() == original
    finally:
        patches.uninstall()
    assert original_fn.__code__ is original_code and original_fn() == original
    patches.install()
    patches.uninstall()
    print('PASS: 12 assembly functions; %s templates; runtime install, fallback and restore' % len(DEFAULTS))


if __name__ == '__main__':
    main()
