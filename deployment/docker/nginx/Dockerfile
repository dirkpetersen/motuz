# Build as
# docker build -t fredhutch/motuz_nginx:latest -f deployment/docker/nginx/Dockerfile .


FROM node:22-alpine AS build-stage

WORKDIR /app
COPY ./package.json ./package-lock.json /app/
RUN npm ci --no-audit --no-fund

COPY ./src/frontend /app/src/frontend
RUN npm run build

FROM nginx:1.28-alpine

COPY --from=build-stage /app/build/ /var/www/
COPY ./deployment/docker/nginx/security_headers.conf /etc/nginx/security_headers.conf
COPY ./deployment/docker/nginx/nginx.conf /etc/nginx/nginx.conf

EXPOSE 80 443
CMD ["nginx","-g","daemon off;"]
