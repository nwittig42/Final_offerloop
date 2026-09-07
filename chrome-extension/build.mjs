#!/usr/bin/env node

/**
 * Build script for Chrome Extension OAuth Client ID management
 * 
 * Switches between dev and production OAuth client IDs in manifest.json
 * 
 * Usage:
 *   node build.js          # Sets to dev (default)
 *   node build.js --dev    # Sets to dev
 *   node build.js --prod   # Sets to production
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// OAuth Client IDs
const CLIENT_IDS = {
  dev: '184607281467-2tiafme53dd0b5q5k709m90qg161tm7b.apps.googleusercontent.com',
  prod: '184607281467-bv1qomua1ndf3jo0tdmpjvte4ukbkcli.apps.googleusercontent.com'
};

// Determine environment from command line args
const args = process.argv.slice(2);
const isProd = args.includes('--prod');
const isDev = args.includes('--dev') || !isProd; // Default to dev

const environment = isProd ? 'prod' : 'dev';
const clientId = CLIENT_IDS[environment];

// Path to manifest.json
const manifestPath = path.join(__dirname, 'manifest.json');

// Read manifest.json
let manifest;
try {
  const manifestContent = fs.readFileSync(manifestPath, 'utf8');
  manifest = JSON.parse(manifestContent);
} catch (error) {
  console.error('Error reading manifest.json:', error.message);
  process.exit(1);
}

// Update client ID
manifest.oauth2.client_id = clientId;

// Write back to manifest.json
try {
  fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + '\n', 'utf8');
  console.log(`✓ Updated manifest.json with ${environment} OAuth client ID`);
  console.log(`  Client ID: ${clientId}`);
} catch (error) {
  console.error('Error writing manifest.json:', error.message);
  process.exit(1);
}