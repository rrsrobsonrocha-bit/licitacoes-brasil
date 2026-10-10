import { PrismaClient } from '@prisma/client';
import * as crypto from 'crypto';

const prisma = new PrismaClient();

function hashPassword(password: string): string {
  const salt = crypto.randomBytes(16).toString('hex');
  const hash = crypto.scryptSync(password, salt, 64).toString('hex');
  return `scrypt:${salt}:${hash}`;
}

async function main() {
  console.log('Populando banco de dados com sementes oficiais...');

  // Criar Usuários
  const admin = await prisma.user.upsert({
    where: { email: 'admin@licitacoesbrasil.gov.br' },
    update: {},
    create: {
      name: 'Administrador do Sistema',
      email: 'admin@licitacoesbrasil.gov.br',
      passwordHash: hashPassword('AdminSeguro!2025'),
      role: 'ADMIN',
      status: 'ACTIVE',
    },
  });

  const demoUser = await prisma.user.upsert({
    where: { email: 'usuario@empresa.com.br' },
    update: {},
    create: {
      name: 'Maria Silva Compras',
      email: 'usuario@empresa.com.br',
      passwordHash: hashPassword('UsuarioSenha@123'),
      role: 'USER',
      status: 'ACTIVE',
    },
  });

  // Criar Fontes Oficiais
  const pncp = await prisma.source.upsert({
    where: { id: 'pncp-source-01' },
    update: {},
    create: {
      id: 'pncp-source-01',
      name: 'Portal Nacional de Contratações Públicas (PNCP)',
      baseUrl: 'https://pncp.gov.br/api/consulta',
      officialDomain: 'pncp.gov.br',
      sourceType: 'FEDERAL',
      integrationType: 'API_OFICIAL',
      active: true,
      authorizationNotes: 'API Pública aberta instituída pela Lei nº 14.133/2021',
      lastSyncStatus: 'SUCESSO',
    },
  });

  const comprasGov = await prisma.source.upsert({
    where: { id: 'comprasgov-source-02' },
    update: {},
    create: {
      id: 'comprasgov-source-02',
      name: 'Compras.gov.br (Comprasnet)',
      baseUrl: 'https://compras.dados.gov.br',
      officialDomain: 'gov.br',
      sourceType: 'FEDERAL',
      integrationType: 'API_OFICIAL',
      active: true,
      authorizationNotes: 'Dados abertos oficiais do Governo Federal',
    },
  });

  console.log('Seed inicial executado com sucesso.');
}

main()
  .catch((e) => {
    console.error(e);
    process.exit(1);
  })
  .finally(async () => {
    await prisma.$disconnect();
  });
