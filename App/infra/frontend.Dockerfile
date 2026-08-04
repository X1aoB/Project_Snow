FROM nginx:1.27-alpine
COPY infra/nginx.conf /etc/nginx/conf.d/default.conf
COPY frontend /usr/share/nginx/html
