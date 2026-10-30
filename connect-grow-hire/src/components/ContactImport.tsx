import React, { useState, useCallback, useRef } from 'react';
import { Button } from "@/components/ui/button";
import {
  Upload,
  AlertCircle,
  Loader2,
  Download,
  X,
  ArrowRight,
  CreditCard,
  Table2,
  CheckCircle,
  Info,
} from "lucide-react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useFirebaseAuth } from '../contexts/FirebaseAuthContext';
import { useNavigate } from 'react-router-dom';
import { auth } from '@/lib/firebase';

import { BACKEND_URL as API_BASE } from '@/services/api';

// Our schema fields that can be mapped
const SCHEMA_FIELDS = [
  { value: 'firstName', label: 'First Name' },
  { value: 'lastName', label: 'Last Name' },
  { value: 'email', label: 'Email' },
  { value: 'linkedinUrl', label: 'LinkedIn URL' },
  { value: 'company', label: 'Company' },
  { value: 'jobTitle', label: 'Job Title' },
  { value: 'college', label: 'College/University' },
  { value: 'location', label: 'Location' },
  { value: 'phone', label: 'Phone' },
  { value: '_skip', label: '(Skip this column)' },
];

interface PreviewData {
  headers: string[];
  column_mapping: Record<string, string>;
  unmapped_headers: string[];
  total_rows: number;
  valid_rows: number;
  sample_contacts: any[];
  credits: {
    available: number;
    cost_per_contact: number;
    total_cost: number;
    can_afford: boolean;
    max_affordable: number;
  };
  enrichment?: {
    contacts_with_email: number;
    contacts_needing_enrichment: number;
    contacts_needing_enrichment_total: number;
    contacts_unenrichable: number;
    enrichment_cap: number;
  };
}

interface ImportResult {
  success: boolean;
  created: number;
  skipped: {
    duplicate: number;
    invalid: number;
    no_credits: number;
    total: number;
  };
  credits: {
    spent: number;
    remaining: number;
  };
  enrichment?: {
    enriched: number;
    failed: number;
    capped: number;
  };
  drafts?: {
    created: number;
    failed: number;
    total_eligible: number;
  };
  warnings?: string[];
}

interface ContactImportProps {
  onImportComplete?: () => void;
  onSwitchTab?: (tab: string) => void;
}

const ContactImport: React.FC<ContactImportProps> = ({ onImportComplete, onSwitchTab }) => {
  const { user, updateCredits } = useFirebaseAuth();
  const navigate = useNavigate();
  const fileInputRef = useRef<HTMLInputElement>(null);

  // State
  const [file, setFile] = useState<File | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [step, setStep] = useState<'upload' | 'preview' | 'result'>('upload');
  
  // Preview data
  const [previewData, setPreviewData] = useState<PreviewData | null>(null);
  const [columnMapping, setColumnMapping] = useState<Record<string, string>>({});
  
  // Import result
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  
  // Upgrade dialog
  const [showUpgradeDialog, setShowUpgradeDialog] = useState(false);

  const getIdToken = async (): Promise<string> => {
    const firebaseUser = auth.currentUser;
    if (!firebaseUser) {
      throw new Error('Not authenticated');
    }
    return await firebaseUser.getIdToken(true);
  };

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    
    const droppedFile = e.dataTransfer.files[0];
    if (droppedFile) {
      validateAndSetFile(droppedFile);
    }
  }, []);

  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFile = e.target.files?.[0];
    if (selectedFile) {
      validateAndSetFile(selectedFile);
    }
  }, []);

  const validateAndSetFile = (file: File) => {
    const validExtensions = ['.csv', '.xlsx', '.xls'];
    const extension = file.name.toLowerCase().slice(file.name.lastIndexOf('.'));
    
    if (!validExtensions.includes(extension)) {
      setError('Please upload a CSV or Excel file (.csv, .xlsx, .xls)');
      return;
    }
    
    setError(null);
    setFile(file);
  };

  const handlePreview = async () => {
    if (!file || !user) return;
    
    setIsLoading(true);
    setError(null);
    
    try {
      const token = await getIdToken();
      const formData = new FormData();
      formData.append('file', file);
      
      const response = await fetch(`${API_BASE}/api/contacts/import/preview`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
        },
        body: formData,
      });
      
      const data = await response.json();
      
      if (!response.ok) {
        if (data.upgrade_required) {
          setShowUpgradeDialog(true);
          return;
        }
        throw new Error(data.error || 'Failed to preview file');
      }
      
      setPreviewData(data);
      setColumnMapping(data.column_mapping);
      setStep('preview');
      
    } catch (err: any) {
      setError(err.message || 'Failed to preview file');
    } finally {
      setIsLoading(false);
    }
  };

  const handleImport = async () => {
    if (!file || !user || !previewData) return;
    
    setIsLoading(true);
    setError(null);
    
    try {
      const token = await getIdToken();
      const formData = new FormData();
      formData.append('file', file);
      formData.append('column_mapping', JSON.stringify(columnMapping));
      
      const response = await fetch(`${API_BASE}/api/contacts/import`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
        },
        body: formData,
      });
      
      const data = await response.json();
      
      if (!response.ok) {
        if (data.upgrade_required) {
          setShowUpgradeDialog(true);
          return;
        }
        throw new Error(data.error || 'Failed to import contacts');
      }
      
      setImportResult(data);
      setStep('result');
      
      // Update user's credits in context
      if (updateCredits && data.credits?.remaining !== undefined) {
        await updateCredits(data.credits.remaining);
      }
      
      if (onImportComplete) {
        onImportComplete();
      }
      
    } catch (err: any) {
      setError(err.message || 'Failed to import contacts');
    } finally {
      setIsLoading(false);
    }
  };

  const handleDownloadTemplate = async () => {
    if (!user) return;
    
    try {
      const token = await getIdToken();
      const response = await fetch(`${API_BASE}/api/contacts/import/template`, {
        headers: {
          'Authorization': `Bearer ${token}`,
        },
      });
      
      if (!response.ok) {
        throw new Error('Failed to download template');
      }
      
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'contact_import_template.csv';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      window.URL.revokeObjectURL(url);
      
    } catch (err: any) {
      setError(err.message || 'Failed to download template');
    }
  };

  const handleColumnMappingChange = (headerIndex: string, field: string) => {
    setColumnMapping(prev => {
      const newMapping = { ...prev };
      if (field === '_skip') {
        delete newMapping[headerIndex];
      } else {
        newMapping[headerIndex] = field;
      }
      return newMapping;
    });
  };

  const resetImport = () => {
    setFile(null);
    setPreviewData(null);
    setColumnMapping({});
    setImportResult(null);
    setError(null);
    setStep('upload');
  };

  const clearFile = () => {
    setFile(null);
    setError(null);
  };

  // Guidelines expandable state
  const [showGuidelines, setShowGuidelines] = useState(false);

  // ==================== UPLOAD STEP ====================
  if (step === 'upload') {
    return (
      <div>
        {/* Main Upload Card — matches LinkedIn tab card styling */}
        <div 
          style={{
            maxWidth: '560px',
            margin: '0 auto',
            textAlign: 'center',
          }}
          className="w-full px-4 py-2 sm:px-6"
        >
          <div className="w-12 h-12 bg-white/70 backdrop-blur-sm rounded-[3px] flex items-center justify-center mx-auto mb-5 border border-black/[0.07] shadow-sm">
            <Upload className="w-5 h-5 text-[#3B82F6]" />
          </div>

          <h2 className="text-[32px] font-normal text-gray-900 mb-2" style={{ fontFamily: "'Instrument Serif', Georgia, serif", letterSpacing: '-0.025em', lineHeight: 1.1 }}>Import Contacts</h2>
          <p className="text-gray-500 mb-8 max-w-md mx-auto">
            Upload a CSV or Excel file to find emails, generate drafts, and save contacts to your library.
          </p>

          {/* Drop zone — clean, no dashed border */}
          <div className="max-w-xl mx-auto">
            <div
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onDrop={handleDrop}
              onClick={() => fileInputRef.current?.click()}
              className={`
                relative rounded-[3px] p-10 text-center cursor-pointer
                transition-all duration-200 border border-dashed
                ${isDragging 
                  ? 'bg-[#FAFBFF] border-[#3B82F6] shadow-[0_0_0_4px_rgba(59,130,246,0.08)]' 
                  : file 
                    ? 'bg-white/60 border-black/10' 
                    : 'bg-white/50 border-black/10 hover:bg-white/70 hover:border-[#3B82F6]/50'
                }
                backdrop-blur-sm
              `}
            >
              <input
                ref={fileInputRef}
                type="file"
                accept=".csv,.xlsx,.xls"
                onChange={handleFileSelect}
                className="hidden"
              />
              
              {!file ? (
                <>
                  <Table2 className={`w-8 h-8 mx-auto mb-3 ${isDragging ? 'text-[#3B82F6]' : 'text-gray-400'}`} />
                  <p className="text-sm font-medium text-gray-900 mb-1">
                    {isDragging ? 'Drop it here!' : 'Drop your spreadsheet here'}
                  </p>
                  <p className="text-xs text-gray-500 mb-4">or click to browse your files</p>
                  <Button
                    className="h-10 px-6 rounded-full bg-white/80 backdrop-blur-sm border border-[#E2E8F0] text-[#3B82F6] font-semibold text-sm hover:bg-[#0F172A] hover:text-white hover:border-[#0F172A] transition-all shadow-none pointer-events-none"
                  >
                    Choose File
                  </Button>
                  <p className="text-xs text-gray-400 mt-4">Supports CSV, XLSX, XLS</p>
                </>
              ) : (
                <>
                  <div className="w-12 h-12 mx-auto mb-3 rounded-[3px] bg-green-100 flex items-center justify-center">
                    <CheckCircle className="w-6 h-6 text-green-600" />
                  </div>
                  <p className="text-sm font-medium text-gray-900 mb-0.5">{file.name}</p>
                  <p className="text-xs text-gray-500 mb-3">{(file.size / 1024).toFixed(1)} KB</p>
                  <button 
                    onClick={(e) => { e.stopPropagation(); clearFile(); }}
                    className="text-sm text-[#3B82F6] hover:underline"
                  >
                    Choose a different file
                  </button>
                </>
              )}
            </div>

            {/* Secondary links row */}
            <div className="flex items-center justify-center gap-4 mt-4 text-sm">
              <button
                onClick={handleDownloadTemplate}
                className="text-[#3B82F6] hover:underline inline-flex items-center gap-1"
              >
                <Download className="w-3.5 h-3.5" />
                Download template
              </button>
              <span className="text-gray-300">·</span>
              <button
                onClick={() => setShowGuidelines(!showGuidelines)}
                className="text-gray-500 hover:text-gray-700 hover:underline inline-flex items-center gap-1"
              >
                <Info className="w-3.5 h-3.5" />
                Import guidelines
              </button>
            </div>

            {/* Expandable guidelines */}
            {showGuidelines && (
              <div className="mt-4 text-left bg-[#FAFBFF] rounded-[3px] p-4 text-xs text-gray-600 space-y-1.5">
                <p><span className="font-medium text-gray-700">10 credits per contact</span> — includes email lookup & AI draft</p>
                <p><span className="font-medium text-gray-700">Duplicates auto-skipped</span> — matching email or LinkedIn URL</p>
                <p><span className="font-medium text-gray-700">Minimum requirements</span> — name, email, or LinkedIn URL</p>
                <p><span className="font-medium text-gray-700">Supported formats</span> — CSV, XLSX, XLS files</p>
              </div>
            )}

            {/* Error */}
            {error && (
              <div className="mt-4 p-3 bg-red-50 text-red-600 text-sm rounded-[3px] flex items-center gap-2 justify-center">
                <AlertCircle className="w-4 h-4" />
                {error}
              </div>
            )}

            {/* CTA Button (when file uploaded) */}
            {file && (
              <div className="mt-6">
                <Button
                  onClick={handlePreview}
                  disabled={isLoading}
                  className="h-12 px-8 rounded-[3px] bg-[#0F172A] hover:bg-[#1E293B] text-white font-medium shadow-md hover:shadow-lg transition-all"
                >
                  {isLoading ? (
                    <Loader2 className="w-5 h-5 animate-spin" />
                  ) : (
                    'Preview Import'
                  )}
                </Button>
              </div>
            )}
          </div>

          {/* Alternative actions — subtle text links */}
          <div className="mt-10 pt-6 border-t border-[#EEF2F8]">
            <p className="text-xs text-gray-400 mb-2">Or try another way</p>
            <div className="flex items-center justify-center gap-4 text-sm">
              <button 
                onClick={() => onSwitchTab?.('contact-search')}
                className="text-gray-500 hover:text-[#3B82F6] hover:underline transition-colors"
              >
                Search for people
              </button>
              <span className="text-gray-300">·</span>
              <button 
                onClick={() => onSwitchTab?.('contact-search')}
                className="text-gray-500 hover:text-[#3B82F6] hover:underline transition-colors"
              >
                Search or paste a LinkedIn URL
              </button>
            </div>
          </div>
        </div>

        {/* Upgrade Dialog */}
        <AlertDialog open={showUpgradeDialog} onOpenChange={setShowUpgradeDialog}>
          <AlertDialogContent className="rounded-[3px]">
            <AlertDialogHeader>
              <AlertDialogTitle>Upgrade to Import Contacts</AlertDialogTitle>
              <AlertDialogDescription>
                Contact import is available for Pro and Elite tier users. Upgrade your plan to import contacts from spreadsheets.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel className="rounded-full">Cancel</AlertDialogCancel>
              <AlertDialogAction
                onClick={() => navigate('/pricing')}
                className="text-white rounded-full bg-gradient-to-r from-[#0F172A] to-[#1E293B]"
              >
                Upgrade to Pro/Elite
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    );
  }

  // ==================== PREVIEW STEP ====================
  if (step === 'preview' && previewData) {
    return (
      <div className="space-y-6">
        {/* Main Preview Card */}
        <div className="bg-transparent rounded-none shadow-none border-none overflow-hidden animate-fadeInUp">
          {/* Subtle top accent line */}
          <div className="h-px bg-gradient-to-r from-transparent via-[#3B82F6]/40 to-transparent mb-8"></div>
          
          <div className="p-8">
            {/* Header */}
            <div className="flex items-center justify-between mb-6">
              <div>
                <h2 className="text-xl font-semibold text-gray-900">Preview Import</h2>
                <p className="text-gray-600 mt-1">Review column mappings and confirm import</p>
              </div>
              <button 
                onClick={resetImport}
                className="flex items-center gap-2 px-4 py-2.5 bg-[#FAFBFF] border border-gray-200 rounded-[3px] text-sm font-medium text-gray-700 hover:bg-gray-100 transition-all"
              >
                <X className="w-4 h-4" />
                Cancel
              </button>
            </div>

            {/* Stats Cards */}
            <div className="grid grid-cols-3 gap-4 mb-6">
              <div className="bg-gradient-to-br from-[#FAFBFF] to-[#EEF2F8] rounded-[3px] p-4 text-center">
                <p className="text-2xl font-bold text-gray-900">{previewData.total_rows}</p>
                <p className="text-sm text-gray-500">Total rows</p>
              </div>
              <div className="bg-gradient-to-br from-green-50 to-emerald-100 rounded-[3px] p-4 text-center">
                <p className="text-2xl font-bold text-green-600">{previewData.valid_rows}</p>
                <p className="text-sm text-gray-500">Valid contacts</p>
              </div>
              <div className="bg-gradient-to-br from-[#FAFBFF] to-[rgba(59,130,246,0.10)] rounded-[3px] p-4 text-center">
                <p className="text-2xl font-bold text-[#3B82F6]">{previewData.credits.available}</p>
                <p className="text-sm text-gray-500">Credits available</p>
              </div>
            </div>

            {/* Credit Cost */}
            <div className={`rounded-[3px] p-4 mb-6 ${previewData.credits.can_afford ? 'bg-[#FAFBFF] border border-[#E2E8F0]' : 'bg-yellow-50 border border-yellow-200'}`}>
              <div className="flex items-center gap-3">
                <div className={`w-10 h-10 rounded-[3px] flex items-center justify-center ${previewData.credits.can_afford ? 'bg-[rgba(59,130,246,0.10)]' : 'bg-yellow-100'}`}>
                  <CreditCard className={`h-5 w-5 ${previewData.credits.can_afford ? 'text-[#3B82F6]' : 'text-yellow-600'}`} />
                </div>
                <div>
                  <p className="font-medium text-gray-900">
                    {previewData.credits.can_afford 
                      ? `This import will use ${previewData.credits.total_cost} credits`
                      : `Not enough credits for all contacts`
                    }
                  </p>
                  <p className="text-sm text-gray-600">
                    {previewData.credits.can_afford 
                      ? `${previewData.valid_rows} contacts × ${previewData.credits.cost_per_contact} credits each — includes email lookup & AI draft`
                      : `You can import up to ${previewData.credits.max_affordable} contacts with your current credits`
                    }
                  </p>
                </div>
              </div>
            </div>

            {/* Enrichment stats (when available) */}
            {previewData.enrichment && (
              <div className="bg-[#FAFBFF] rounded-[3px] border border-gray-200 p-4 mb-6">
                <h3 className="font-medium text-gray-900 mb-2">What we&apos;ll do</h3>
                <ul className="text-sm text-gray-600 space-y-1">
                  <li>{previewData.enrichment.contacts_with_email} contacts already have emails</li>
                  <li>{previewData.enrichment.contacts_needing_enrichment_total} contacts will be enriched via LinkedIn (email lookup)</li>
                  <li>{previewData.enrichment.contacts_unenrichable} contacts have no email or LinkedIn — imported as-is</li>
                  {previewData.enrichment.contacts_needing_enrichment_total > (previewData.enrichment.enrichment_cap ?? 50) && (
                    <li className="text-amber-700 font-medium">Up to {previewData.enrichment.enrichment_cap ?? 50} contacts will be enriched per import</li>
                  )}
                </ul>
              </div>
            )}

            {/* Column Mapping */}
            <div className="bg-[#FAFBFF] rounded-[3px] border border-gray-200 overflow-hidden mb-6">
              <div className="px-4 py-3 border-b border-gray-200 bg-gradient-to-r from-[#EEF2F8] to-[#EEF2F8]">
                <h3 className="font-medium text-gray-900">Column Mapping</h3>
                <p className="text-sm text-gray-500">Adjust how your spreadsheet columns map to contact fields</p>
              </div>
              <div className="p-4 space-y-3 bg-white">
                {previewData.headers.map((header, idx) => (
                  <div key={idx} className="flex items-center gap-4">
                    <div className="w-1/3">
                      <p className="text-sm font-medium text-gray-700 truncate" title={header}>
                        {header || `(Column ${idx + 1})`}
                      </p>
                    </div>
                    <ArrowRight className="h-4 w-4 text-gray-400 flex-shrink-0" />
                    <div className="w-1/2">
                      <Select
                        value={columnMapping[idx.toString()] || '_skip'}
                        onValueChange={(value) => handleColumnMappingChange(idx.toString(), value)}
                      >
                        <SelectTrigger className="w-full rounded-[3px] border-gray-300">
                          <SelectValue placeholder="Select field..." />
                        </SelectTrigger>
                        <SelectContent>
                          {SCHEMA_FIELDS.map(field => (
                            <SelectItem key={field.value} value={field.value}>
                              {field.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Sample Preview Table */}
            {previewData.sample_contacts.length > 0 && (
              <div className="bg-[#FAFBFF] rounded-[3px] border border-gray-200 overflow-hidden mb-6">
                <div className="px-4 py-3 border-b border-gray-200 bg-gradient-to-r from-[#EEF2F8] to-[#EEF2F8]">
                  <h3 className="font-medium text-gray-900">Sample Preview</h3>
                  <p className="text-sm text-gray-500">First {previewData.sample_contacts.length} contacts</p>
                </div>
                <div className="overflow-x-auto bg-white">
                  <table className="min-w-full divide-y divide-gray-200">
                    <thead className="bg-[#FAFBFF]">
                      <tr>
                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Name</th>
                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Email</th>
                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Company</th>
                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Title</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-200">
                      {previewData.sample_contacts.map((contact, idx) => (
                        <tr key={idx} className="hover:bg-[#FAFBFF]">
                          <td className="px-4 py-3 text-sm text-gray-900">
                            {contact.firstName} {contact.lastName}
                          </td>
                          <td className="px-4 py-3 text-sm text-gray-600">{contact.email || '—'}</td>
                          <td className="px-4 py-3 text-sm text-gray-600">{contact.company || '—'}</td>
                          <td className="px-4 py-3 text-sm text-gray-600">{contact.jobTitle || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* Error */}
            {error && (
              <div className="mb-6 flex items-center gap-2 bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-[3px]">
                <AlertCircle className="h-5 w-5 flex-shrink-0" />
                <p>{error}</p>
              </div>
            )}

            {/* Loading note when importing */}
            {isLoading && (
              <p className="text-sm text-gray-500 text-center mb-4">
                This may take a minute — we&apos;re looking up emails and drafting messages.
              </p>
            )}

            {/* Actions */}
            <div className="flex flex-col sm:flex-row justify-center gap-3">
              <button 
                onClick={resetImport}
                className="px-6 py-3 bg-white border border-gray-200 rounded-full text-gray-700 font-medium hover:bg-[#FAFBFF] transition-all"
              >
                Cancel
              </button>
              <button
                onClick={handleImport}
                disabled={isLoading || previewData.valid_rows === 0}
                className={`
                  px-8 py-3 rounded-full font-semibold flex items-center justify-center gap-2
                  transition-all duration-200 transform
                  ${isLoading || previewData.valid_rows === 0
                    ? 'bg-gray-300 text-gray-500 cursor-not-allowed'
                    : 'bg-gradient-to-r from-[#0F172A] to-[#1E293B] text-white shadow-lg shadow-[#3B82F6]/30 hover:shadow-xl hover:shadow-[#3B82F6]/40 hover:scale-105 active:scale-100'
                  }
                `}
              >
                {isLoading ? (
                  <>
                    <Loader2 className="w-5 h-5 animate-spin" />
                    Importing & generating drafts...
                  </>
                ) : (
                  <>
                    Import {previewData.credits.can_afford ? previewData.valid_rows : previewData.credits.max_affordable} Contacts
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // ==================== RESULT STEP ====================
  if (step === 'result' && importResult) {
    const detailRows: { label: string; value: number; accent: boolean }[] = [];
    if ((importResult.enrichment?.enriched ?? 0) > 0) {
      detailRows.push({ label: 'Emails found via LinkedIn', value: importResult.enrichment!.enriched, accent: true });
    }
    if ((importResult.drafts?.created ?? 0) > 0) {
      detailRows.push({ label: 'Email drafts created', value: importResult.drafts!.created, accent: true });
    }
    if ((importResult.skipped?.invalid ?? 0) > 0) {
      detailRows.push({ label: 'Skipped — invalid rows', value: importResult.skipped.invalid, accent: false });
    }
    if ((importResult.skipped?.duplicate ?? 0) > 0) {
      detailRows.push({ label: 'Skipped — duplicates', value: importResult.skipped.duplicate, accent: false });
    }
    if ((importResult.skipped?.no_credits ?? 0) > 0) {
      detailRows.push({ label: 'Skipped — no credits', value: importResult.skipped.no_credits, accent: false });
    }
    if ((importResult.enrichment?.failed ?? 0) > 0) {
      detailRows.push({ label: 'LinkedIn lookups — no email found', value: importResult.enrichment!.failed, accent: false });
    }
    if ((importResult.enrichment?.capped ?? 0) > 0) {
      detailRows.push({ label: 'Exceeded enrichment limit', value: importResult.enrichment!.capped, accent: false });
    }
    if ((importResult.drafts?.failed ?? 0) > 0) {
      detailRows.push({ label: 'Email drafts failed', value: importResult.drafts!.failed, accent: false });
    }

    const handleViewInTracker = () => {
      onImportComplete?.();
      // Imported contacts live on My Network → People (the inner
      // contact-library tab was removed in the IA cleanup).
      navigate('/contacts');
    };

    return (
      <div className="space-y-6">
        <div
          style={{
            maxWidth: '520px',
            margin: '0 auto',
            padding: '0',
          }}
        >
          {/* Success header */}
          <div style={{ marginBottom: 24 }}>
            <div
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 8,
                background: '#F0FDF4',
                borderRadius: '20px',
                padding: '6px 14px',
                marginBottom: 16,
              }}
            >
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="#15803D" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M4.5 7L6.5 9L9.5 5" />
              </svg>
              <span style={{ fontSize: 13, fontWeight: 500, color: '#15803D' }}>Import complete</span>
            </div>
            <div style={{ fontSize: 56, fontWeight: 700, color: '#111827', letterSpacing: '-0.04em', lineHeight: 1 }}>
              {importResult.created}
            </div>
            <div style={{ fontSize: 15, color: '#6B7280', fontWeight: 400, marginTop: 8 }}>
              contacts added to your library
            </div>
          </div>

          {importResult.warnings && importResult.warnings.length > 0 && (
            <div style={{
              background: '#FEF3C7',
              border: '1px solid #F59E0B',
              borderRadius: 8,
              padding: '10px 14px',
              marginBottom: 8,
            }}>
              {importResult.warnings.map((w, i) => (
                <div key={i} style={{ fontSize: 13, color: '#92400E', lineHeight: 1.4 }}>{w}</div>
              ))}
            </div>
          )}

          {detailRows.length > 0 && (
            <>
              <div style={{ height: 1, background: 'rgba(0,0,0,0.06)', margin: '0 0' }} />
              <div style={{ paddingTop: 4 }}>
                {detailRows.map((row) => (
                  <div
                    key={row.label}
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      padding: '11px 0',
                    }}
                  >
                    <span style={{ fontSize: 14, color: '#6B7280', fontWeight: 400 }}>{row.label}</span>
                    <span
                      style={{
                        fontSize: 14,
                        fontWeight: 600,
                        fontVariantNumeric: 'tabular-nums',
                        color: row.accent ? '#3B82F6' : '#9CA3AF',
                      }}
                    >
                      {row.value}
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}

          <div style={{ height: 1, background: 'rgba(59, 130, 246, 0.06)', margin: '0 -40px 0 -40px' }} />
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              padding: '14px 0',
            }}
          >
            <span style={{ fontSize: 13, color: '#9CA3AF' }}>Credits used</span>
            <span style={{ fontSize: 13, fontWeight: 600, color: '#111827' }}>
              {importResult.credits.spent}
              <span style={{ color: '#D1D5DB', margin: '0 6px' }}>·</span>
              <span style={{ fontSize: 12, color: '#9CA3AF', fontWeight: 400 }}>
                {importResult.credits.remaining} remaining
              </span>
            </span>
          </div>

          <div style={{ height: 1, background: 'rgba(0,0,0,0.06)', margin: '0' }} />
          <div
            style={{
              display: 'flex',
              gap: 12,
              padding: '20px 0 28px 0',
            }}
          >
            <button
              type="button"
              onClick={resetImport}
              style={{
                flex: 1,
                height: 44,
                borderRadius: 12,
                border: '1px solid #E5E7EB',
                background: 'transparent',
                color: '#6B7280',
                fontSize: 14,
                fontWeight: 500,
                transition: 'all 0.15s ease',
                cursor: 'pointer',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = '#F9FAFB';
                e.currentTarget.style.color = '#3B82F6';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = 'transparent';
                e.currentTarget.style.color = '#6B7280';
              }}
            >
              Import more
            </button>
            <button
              type="button"
              onClick={handleViewInTracker}
              style={{
                flex: 1,
                height: 44,
                borderRadius: 12,
                border: 'none',
                background: '#0F172A',
                color: '#FFFFFF',
                fontSize: 14,
                fontWeight: 500,
                boxShadow: '0 1px 3px rgba(28, 22, 10, 0.15)',
                transition: 'all 0.2s ease',
                cursor: 'pointer',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = '#1E293B';
                e.currentTarget.style.boxShadow = '0 4px 16px rgba(28, 22, 10, 0.25)';
                e.currentTarget.style.transform = 'translateY(-1px)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = '#0F172A';
                e.currentTarget.style.boxShadow = '0 1px 3px rgba(28, 22, 10, 0.15)';
                e.currentTarget.style.transform = 'translateY(0)';
              }}
            >
              View in Inbox
            </button>
          </div>
        </div>
      </div>
    );
  }

  return null;
};

export default ContactImport;
