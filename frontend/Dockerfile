FROM node:20-alpine

WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npx svelte-kit sync || true
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
