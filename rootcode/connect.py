"""/model: pick a provider, enter a key, pick a model, prove it answers, save it."""

import getpass
import time

from .providers import PROVIDERS, Quiet, api_key, chat, list_models, why, without_tools
from .settings import CONFIG, save_config, state
from .tools import TOOLS
from .ui import ACCENT, BOLD, DIM, GREEN, RED, RESET, YELLOW, ask, spinner


def choose_model():
    names = list(PROVIDERS)
    print(f"\n  {BOLD}Choose a provider{RESET}")
    for i, name in enumerate(names, 1):
        current = f"  {ACCENT}● current{RESET}" if name == state.provider else ""
        print(f"  {DIM}{i}.{RESET} {PROVIDERS[name]['label']}{current}")
    answer = ask(f"  {ACCENT}❯{RESET} {DIM}[1-{len(names)}, enter keeps current]{RESET} ").strip()
    provider = names[int(answer) - 1] if answer.isdigit() and 1 <= int(answer) <= len(names) else state.provider

    if provider == "local":
        current = CONFIG.get("local_url", "http://localhost:1234/v1")
        entered = ask(f"  {BOLD}Server address{RESET} {DIM}(LM Studio :1234/v1, llama.cpp :8080/v1; "
                      f"enter keeps {current}){RESET} ").strip()
        CONFIG["local_url"] = (entered or current).rstrip("/")

    key, new_key = None, False
    if PROVIDERS[provider]["key_env"]:
        have = api_key(provider)
        optional = PROVIDERS[provider].get("key_optional")
        hint = f"enter keeps …{have[-4:]}" if have else ("optional, enter to skip" if optional else "typing is hidden")
        entered = getpass.getpass(f"  {BOLD}{PROVIDERS[provider]['label'].split(' (')[0]} API key{RESET} ({hint}): ").strip()
        key, new_key = entered or have, bool(entered)
        if not key and not optional:
            print(f"  {RED}A key is needed for {provider}.{RESET}")
            return

    spinner.start("Fetching models", time.time())
    try:
        models = list_models(provider, key)
    except Exception as e:
        spinner.stop()
        print(f"  {RED}Could not list models: {why(e)}{RESET}")
        return
    spinner.stop()
    if not models:
        hint = " Pull one first, e.g. ollama pull qwen3.6" if provider == "ollama" else ""
        print(f"  {RED}No models found.{hint}{RESET}")
        return

    toolless = without_tools(provider, models)
    shown, model = models, None
    print(f"\n  {BOLD}Choose a model{RESET} {DIM}({len(models)} available){RESET}")
    while model is None:
        for i, name in enumerate(shown[:30], 1):
            current = f"  {ACCENT}● current{RESET}" if name == state.model and provider == state.provider else ""
            mark = f"  {YELLOW}no tools{RESET}" if name in toolless else ""
            print(f"  {DIM}{i:>2}.{RESET} {name}{current}{mark}")
        if len(shown) > 30:
            print(f"  {DIM}    … {len(shown) - 30} more: type part of a name to narrow the list{RESET}")
        answer = ask(f"  {ACCENT}❯{RESET} {DIM}[number, name or search; enter cancels]{RESET} ").strip()
        if not answer:
            print(f"  {DIM}Cancelled, nothing changed.{RESET}")
            return
        if answer.isdigit() and 1 <= int(answer) <= min(30, len(shown)):
            model = shown[int(answer) - 1]
        elif answer in models:
            model = answer
        else:
            narrowed = [m for m in models if answer.lower() in m.lower()]
            if narrowed:
                shown = narrowed
            else:
                print(f"  {RED}No model matches {answer!r}.{RESET}")

    spinner.start(f"Checking {model}", time.time())
    try:       # offering a tool proves the model can call tools, not just chat
        reply = chat(provider, key, model, "Reply with just: OK", [{"role": "user", "content": "Say OK"}],
                     [TOOLS["read"].spec()], Quiet(), max_tokens=300)
    except Exception as e:
        spinner.stop()
        print(f"  {RED}✗ That didn't work, nothing was changed: {why(e)}{RESET}")
        return
    spinner.stop()
    state.provider, state.model, state.key = provider, model, key
    CONFIG["provider"], CONFIG["model"] = provider, model
    if new_key:
        CONFIG.setdefault("keys", {})[provider] = key
    save_config()
    said = reply.text.strip().replace("\n", " ")[:40]
    print(f"  {GREEN}✓{RESET} {BOLD}{model}{RESET} via {PROVIDERS[provider]['label']} "
          f"{DIM}· answered “{said or '…'}” · saved{RESET}")
