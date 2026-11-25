# Build the static React application.
FROM registry.access.redhat.com/ubi9/nodejs-22:latest AS build

WORKDIR /opt/app-root/src
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# Serve the built assets with UBI NGINX.
FROM registry.access.redhat.com/ubi9/nginx-124:latest

COPY --from=build /opt/app-root/src/dist /opt/app-root/src
COPY nginx/default.conf.template /opt/app-root/etc/nginx.default.d/app.conf.template

EXPOSE 8080

# UBI nginx has a default_server block that includes /opt/app-root/etc/nginx.default.d/*.conf.
# We inject our root + location blocks there (not a competing server block in nginx.d/).
# Only substitute ${BACKEND_URL} to leave nginx vars ($host, $uri, etc.) intact.
CMD ["/bin/bash", "-c", "envsubst '${BACKEND_URL}' < /opt/app-root/etc/nginx.default.d/app.conf.template > /opt/app-root/etc/nginx.default.d/app.conf && exec nginx -g 'daemon off;'"]
