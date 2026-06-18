/**
 * Configuración por ambiente para aws-monitor.
 *
 * DEV:  cuenta 3htp actual — perfil 3htpusa-monitor
 * PROD: cuenta de producción — perfil aws-monitor-prod
 *
 * Para agregar un ambiente nuevo:
 *   1. Agregar entrada aquí
 *   2. Agregar script en package.json
 *   3. Configurar perfil en ~/.aws/credentials
 */

export interface EnvConfig {
  account: string;   // AWS account ID
  region: string;    // Región de deploy
  profile: string;   // Perfil en ~/.aws/credentials
}

export const environments: Record<string, EnvConfig> = {
  dev: {
    account: '369595298303',
    region:  'us-east-1',
    profile: '3htpusa-monitor',
  },
  prod: {
    account: 'PROD_ACCOUNT_ID',   // reemplazar con el account ID real de producción
    region:  'us-east-1',
    profile: 'aws-monitor-prod',
  },
};
