# Offerloop Platform

A comprehensive platform for professional networking, contact management, and email outreach automation.

## Project Overview

Offerloop is a full-stack application that helps users:
- Search and discover professional contacts
- Generate personalized outreach emails
- Manage contact directories
- Prepare for coffee chats and networking events
- Manage subscriptions and billing

## Architecture

This project consists of:

1. **Backend API** (`backend/`) - Flask-based REST API
2. **Frontend Application** (`connect-grow-hire/`) - React SPA with Vite

## Quick Start

### Prerequisites

- Python 3.8+ (for backend)
- Node.js 18+ and npm (for frontend)
- Firebase project configured
- Environment variables configured (see below)

### Setup

1. **Clone the repository**
```bash
git clone <repository-url>
cd Final_offerloop
```

2. **Backend Setup**
```bash
# Install Python dependencies
cd backend
pip install -r requirements.txt

# Set up environment variables (create .env in project root)
# See backend/README.md for required variables

# Start backend server
cd ..
python3 app.py
```

Backend will run on `http://localhost:5001`

3. **Frontend Setup**
```bash
# Install Node dependencies
cd connect-grow-hire
npm install

# Set up environment variables (create .env file)
# See connect-grow-hire/README.md for required variables

# Start development server
npm run dev
```

Frontend will run on `http://localhost:8080`

## Documentation

- **[Backend Documentation](./backend/README.md)** - Complete API documentation, architecture, and setup guide
- **[Frontend Documentation](./connect-grow-hire/README.md)** - Frontend architecture, components, and development guide

## Project Structure

```
Final_offerloop/
├── backend/                 # Flask backend API
│   ├── app/
│   │   ├── routes/         # API endpoints
│   │   ├── services/       # Business logic
│   │   ├── models/         # Data models
│   │   └── utils/          # Utilities
│   ├── wsgi.py             # WSGI entry point
│   └── README.md           # Backend documentation
├── connect-grow-hire/       # React frontend
│   ├── src/
│   │   ├── components/     # React components
│   │   ├── pages/          # Page components
│   │   ├── services/       # API clients
│   │   └── ...
│   ├── dist/               # Production build
│   └── README.md           # Frontend documentation
├── app.py                  # Backend entry point (shim)
└── README.md               # This file
```

## Environment Variables

Create a `.env` file in the project root with:

### Backend Variables
```bash
# API Keys
OPENAI_API_KEY=your_key
PEOPLE_DATA_LABS_API_KEY=your_key
STRIPE_SECRET_KEY=your_key
STRIPE_PUBLISHABLE_KEY=your_key
STRIPE_WEBHOOK_SECRET=your_key
SERPAPI_KEY=your_key

# Google OAuth
GOOGLE_CLIENT_ID=your_id
GOOGLE_CLIENT_SECRET=your_secret

# Firebase
GOOGLE_APPLICATION_CREDENTIALS=path/to/firebase-credentials.json

# Flask
FLASK_SECRET=your_secret
FLASK_ENV=development
```

### Frontend Variables
Create `.env` in `connect-grow-hire/`:
```bash
VITE_API_BASE_URL=http://localhost:5001/api
VITE_FIREBASE_API_KEY=your_key
VITE_FIREBASE_AUTH_DOMAIN=your_domain
VITE_FIREBASE_PROJECT_ID=your_project_id
VITE_FIREBASE_STORAGE_BUCKET=your_bucket
VITE_FIREBASE_MESSAGING_SENDER_ID=your_sender_id
VITE_FIREBASE_APP_ID=your_app_id
```

## Development Workflow

1. **Start Backend** (Terminal 1)
```bash
python3 app.py
```

2. **Start Frontend** (Terminal 2)
```bash
cd connect-grow-hire
npm run dev
```

3. **Access Application**
- Frontend: http://localhost:8080
- Backend API: http://localhost:5001
- Health Check: http://localhost:5001/health

## Key Features

### Contact Management
- Search contacts by job title, company, location
- Free tier (3 contacts) and Pro tier (8 contacts)
- Contact directory with email tracking
- Resume-based personalization (Pro tier)

### Email Generation
- AI-powered email generation
- Gmail integration
- Draft creation
- Reply tracking

### Coffee Chat Preparation
- LinkedIn profile analysis
- Similarity detection
- Question generation
- PDF export

### Billing & Subscriptions
- Stripe integration
- Free and Pro tiers
- Credit system
- Subscription management

## Technology Stack

### Backend
- Flask (Python web framework)
- Firebase/Firestore (Database & Auth)
- OpenAI (AI features)
- People Data Labs (Contact enrichment)
- Stripe (Payments)
- Gmail API (Email)

### Frontend
- React 18
- TypeScript
- Vite
- React Router
- Firebase SDK
- TanStack Query
- Tailwind CSS
- shadcn/ui

## API Endpoints

See [Backend README](./backend/README.md) for complete API documentation.

Key endpoints:
- `GET /health` - Health check
- `POST /api/free-run` - Free tier contact search
- `POST /api/pro-run` - Pro tier contact search
- `GET /api/contacts` - Get user contacts
- `POST /api/coffee-chat-prep` - Create coffee chat prep
- `POST /api/create-checkout-session` - Stripe checkout

## Deployment

### Backend
- WSGI server (Gunicorn, uWSGI, etc.)
- Environment variables configured
- Firebase credentials set up

### Frontend
- Build: `npm run build` in `connect-grow-hire/`
- Serve `dist/` folder (or let backend serve it)
- Configure production API URL

## Troubleshooting

### Backend Issues
- Check environment variables are loaded
- Verify Firebase credentials path
- Check CORS configuration matches frontend origin
- See [Backend README](./backend/README.md)

### Frontend Issues
- Verify backend is running
- Check API base URL configuration
- Verify Firebase configuration
- See [Frontend README](./connect-grow-hire/README.md)

## Contributing

[Add contribution guidelines]

## License

[Your License Here]

## Support

For detailed documentation:
- Backend: See [backend/README.md](./backend/README.md)
- Frontend: See [connect-grow-hire/README.md](./connect-grow-hire/README.md)
test
