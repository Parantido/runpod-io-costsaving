# Hooks

Commands referenced by schedule `pre_hooks` and `post_hooks` live here.
The same files are copied into the container, and `{root}` in a hook command
resolves to this repository on the host and to `/app` in the container.

Example:

```json
["{root}/hooks/update-webservice.sh", "--pod-id-file", "{root}/data/pods/riva-02.pod_id"]
```

Keep hook commands config/CLI managed; the web UI does not edit them.
