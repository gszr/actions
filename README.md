# gszr/actions

## Runta dispatch

Dispatch a GitHub issue to Pi in a Runta runtime. Trigger the consumer workflow by adding the configured label or commenting `/runta`.

```yaml
- uses: gszr/actions/runta-dispatch@v1
  with:
    runtime: lunar-agent
    model: ${{ vars.RUNTA_MODEL }}
    base_url: ${{ vars.RUNTA_BASE_URL }}
    provider_secret: ${{ vars.RUNTA_PROVIDER_SECRET }}
    gh_token: ${{ github.token }}
  env:
    RUNTA_TOKEN: ${{ secrets.RUNTA_TOKEN }}
```
