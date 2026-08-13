.PHONY: up down logs api frontend install

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f

api:
	cd backend && uvicorn app.main:app --reload --port 8000

frontend:
	cd frontend && npm run dev

install:
	cd backend && pip install -r requirements.txt
	cd frontend && npm install
