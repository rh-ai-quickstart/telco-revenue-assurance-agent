# Build the static React application.
FROM docker.io/library/node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# Serve the built assets with an OpenShift-compatible unprivileged NGINX image.
FROM docker.io/nginxinc/nginx-unprivileged:1.27.4-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template

EXPOSE 8080
