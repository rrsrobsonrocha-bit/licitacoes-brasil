import React from 'react';

export default function Footer() {
  return (
    <footer className="bg-slate-900 text-slate-400 text-xs py-8 border-t border-slate-800">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex flex-col sm:flex-row justify-between items-center text-slate-500">
          <span>© 2025 Licitações Brasil. Sistema aberto e auditável sob a Lei nº 14.133/2021 e LGPD.</span>
          <span className="mt-2 sm:mt-0">Tecnologia Next.js, Prisma & IA Documental.</span>
        </div>
      </div>
    </footer>
  );
}
