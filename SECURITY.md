# Security Policy

## Reporting

Report vulnerabilities privately to the repository maintainer. Do not open a
public issue for a suspected secret leak or authentication bypass.

## Deployment Guidance

- Terminate TLS at a reverse proxy and enable `RUNPOD_OPS_SECURE_COOKIES`.
- Use a long random `ADMIN_PASSWORD` and `SECRET_KEY`.
- Provide the RunPod API key through `RUNPOD_API_KEY` or a mounted key file.
- Never commit `.env`, `config.yaml`, `data/`, logs, or key files.
- Restrict network access to the UI; there is no multi-user role model.
- Rotate the RunPod API key if it may have been exposed in logs or images.

## Data Handling

The SQLite database stores pod IDs, job commands, and job output. Commands
mask token and password values, but engine output may still contain provider
details. Treat `data/` as sensitive.

## Supported Versions

Only the latest v1 release is supported.
