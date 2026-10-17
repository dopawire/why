# why

**`git blame` tells you who. `why` tells you why.**

Point it at a line of code. It follows that line back through every commit that touched it, then to the pull requests, review comments and issues behind those commits. Then it gives you the story, with every claim linked to its source.

```
$ why src/client.py:42-44                    # example output

why src/client.py:42-44
    42 │     for attempt in range(RETRIES):
    43 │         try:
    44 │             return session.get(url, timeout=5)

The loop retries because the upstream API drops a small share of requests
under load [2][3]. RETRIES was lowered from 10 to 3 after retries piled up
during an outage [4].

Before you change it:
- Retrying is only safe because these requests are GETs [3].
- Inference: raising RETRIES again would bring back the pile-up described in [4].

Sources
  [1] commit 2025-03-02  Add HTTP client                         @alice
  [2] commit 2025-04-18  Retry flaky upstream calls (#42)       @bob
  [3] pull   2025-04-18  #42 Retry flaky upstream calls         @bob
  [4] issue  2025-06-09  #57 Retries amplify outage             @carol
```

When the reason was never written down, `why` says so: *"No rationale was recorded."* That's worth knowing too.

## Install

```sh
pipx install why-blame            # core: zero dependencies
pipx install 'why-blame[claude]'  # plus the Claude backend
```

Or from a checkout: `pip install -e .`

## Use

```sh
why path/to/file.py:42          # one line
why path/to/file.py:40-58       # a range
why path/to/file.py -L 40,58    # same thing, git-style
why file.py:42 --offline        # git history only, no network
why file.py:42 --backend none   # just the evidence, no model
why file.py:42 --json           # for editors and scripts
why file.py:42 --web            # open the line in GitHub's blame view
```

## How it works

1. **`git log -L`** finds every commit that changed those exact lines, each with its diff scoped to the range.
2. **GitHub** (if `origin` points there): for each commit, it looks up the PR that merged it, plus any `#123` refs in the message. It pulls in the PR description, reviews, line comments on this file, and the issues the PR links to.
3. **The current file**: the code around your lines, the file's header comments, and the definitions of names your lines use, so the model knows what `FILES` holds and sees rules like "bump CACHE when this list changes".
4. **A model** reads the numbered sources and writes a short explanation. It must cite a source for every claim and label any guess as inference.

## Backends

`--backend auto` (the default) picks the first one that's available:

| Backend  | Needs | Notes |
|----------|-------|-------|
| `claude` | `why-blame[claude]` + `ANTHROPIC_API_KEY` | Uses `claude-opus-5-5`. Override with `--model`. |
| `openrouter` | `OPENROUTER_API_KEY` | Any of OpenRouter's models. Defaults to `anthropic/claude-opus-5.5`; pick another with `--model` or `WHY_OPENROUTER_MODEL`. No extra install. |
| `ollama` | a running [Ollama](https://ollama.com) + `--model` or `WHY_OLLAMA_MODEL` | Fully local. `OLLAMA_HOST` is respected. |
| `none`   | nothing | Prints the evidence list. |

### Set your defaults once

Put these in `~/.bashrc` (or `~/.zshrc`) so every `why` uses them:

```sh
export OPENROUTER_API_KEY=sk-or-...           # your key
export WHY_BACKEND=openrouter                 # claude | openrouter | ollama | none | auto
export WHY_MODEL=anthropic/claude-sonnet-5.5  # any model the backend offers
```

`--backend` and `--model` on the command line still win for a single run. Model precedence: `--model`, then `WHY_MODEL`, then the backend's own variable (`WHY_OPENROUTER_MODEL`, `WHY_OLLAMA_MODEL`), then the built-in default.

GitHub requests use `GITHUB_TOKEN`, `GH_TOKEN`, or `gh auth token` if present. Without a token you get 60 requests an hour.

## Develop

```sh
python -m unittest -v      # no test deps needed
```

The code is small on purpose:

- `whyblame/gitlog.py`: git plumbing
- `whyblame/forge.py`: GitHub
- `whyblame/story.py`: assembles the evidence
- `whyblame/context.py`: picks the surrounding code worth showing
- `whyblame/explain.py`: the model prompt and backends
- `whyblame/cli.py`: the command line

## Roadmap, and good first contributions

- GitLab / Gitea / Forgejo forges (`forge.py` is the only file that needs a sibling)
- On-disk cache of API responses, so repeat runs are instant and work offline
- Parallel GitHub fetches (a cold run is ~10s today)
- Follow code across file renames and moves
- Editor integrations (`--json` is the hook)
- Point at a symbol instead of a line: `why src/retry.py::fetch`

## License

MIT
