// src/config/firebase.ts
import { initializeApp } from 'firebase/app';
import { connectAuthEmulator, getAuth } from 'firebase/auth';
import { connectFirestoreEmulator, getFirestore } from 'firebase/firestore';
import { connectStorageEmulator, getStorage } from 'firebase/storage';  // ← ADD THIS LINE

// Firebase configuration — read from VITE_ env vars with hardcoded fallbacks
// so existing deploys continue to work without setting env vars.
const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY || "AIzaSyCxcZbNwbh09DFw70tBQUSoqBIDaXNwZdE",
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN || "offerloop-native.firebaseapp.com",
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID || "offerloop-native",
  storageBucket: import.meta.env.VITE_FIREBASE_STORAGE_BUCKET || "offerloop-native.firebasestorage.app",
  messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID || "184607281467",
  appId: import.meta.env.VITE_FIREBASE_APP_ID || "1:184607281467:web:eab1b0e8be341aa8c5271e",
};

// Initialize Firebase
const app = initializeApp(firebaseConfig);

// Initialize Firebase Authentication and get a reference to the service
export const auth = getAuth(app);


// Initialize Cloud Firestore and get a reference to the service  
export const db = getFirestore(app);

// Initialize Firebase Storage and get a reference to the service  // ← ADD THIS LINE
export const storage = getStorage(app);                             // ← ADD THIS LINE

// Local end-to-end testing against the Firebase Emulator Suite (see
// scripts/e2e-emulator/README.md). Dev server only, and only when
// VITE_USE_FIREBASE_EMULATOR=1: a production build never connects to an
// emulator, and a plain `npm run dev` keeps talking to the real project.
export const usingFirebaseEmulator =
  import.meta.env.DEV && import.meta.env.VITE_USE_FIREBASE_EMULATOR === "1";
if (usingFirebaseEmulator) {
  const host = import.meta.env.VITE_FIREBASE_EMULATOR_HOST || "127.0.0.1";
  connectAuthEmulator(auth, `http://${host}:${import.meta.env.VITE_AUTH_EMULATOR_PORT || 9099}`, { disableWarnings: true });
  connectFirestoreEmulator(db, host, Number(import.meta.env.VITE_FIRESTORE_EMULATOR_PORT || 8180));
  connectStorageEmulator(storage, host, Number(import.meta.env.VITE_STORAGE_EMULATOR_PORT || 9199));
  console.warn(`[firebase] Using the local Firebase Emulator Suite at ${host} (no production Firebase).`);
}

export default app;
