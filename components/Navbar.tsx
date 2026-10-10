import React from 'react';
import Link from 'next/link';

export default function Navbar() {
  return (
    <header className="bg-slate-900 text-white shadow-md sticky top-0 z-50">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-16">
          <Link href="/" className="flex items-center space-x-3">
            <div className="w-10 h-10 rounded-lg bg-gradient-to-tr from-sky-400 to-emerald-400 flex items-center justify-center text-slate-900 font-black text-xl shadow-inner">
              ⚖
            </div>
            <span className="text-xl font-bold tracking-tight text-white">
              Licitações<span className="text-sky-400">Brasil</span>
            </span>
          </Link>

          <nav className="hidden md:flex items-center space-x-6 text-sm font-medium">
            <Link href="/search" className="text-slate-200 hover:text-white transition">Pesquisar</Link>
            <Link href="/dashboard" className="text-slate-200 hover:text-white transition">Dashboard</Link>
            <Link href="/favorites" className="text-slate-200 hover:text-white transition">Favoritos</Link>
            <Link href="/alerts" className="text-slate-200 hover:text-white transition">Alertas</Link>
            <Link href="/admin" className="text-slate-200 hover:text-white transition">Gestão</Link>
          </nav>

          <div className="flex items-center space-x-3 text-sm">
            <Link href="/login" className="bg-sky-500 hover:bg-sky-600 text-white font-medium px-4 py-2 rounded-lg text-xs tracking-wider uppercase transition shadow-sm">
              Entrar
            </Link>
          </div>
        </div>
      </div>
    </header>
  );
}
