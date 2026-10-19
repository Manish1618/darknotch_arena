# Frontend group image: unprivileged nginx (runs as a normal user on port 8080)
# serving the static website. Build from the repository root:
#   docker build -f deploy/web.Dockerfile .
FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY deploy/security.conf /etc/nginx/snippets/security.conf
COPY web/ /usr/share/nginx/html/
EXPOSE 8080
