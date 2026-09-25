const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '');

async function request(path, options = {}) {
  const url = `${API_BASE_URL}${path}`;

  try {
    const response = await fetch(url, {
      headers: {
        ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
        ...options.headers,
      },
      ...options,
    });

    const contentType = response.headers.get('content-type') || '';
    const data = contentType.includes('application/json') ? await response.json() : await response.text();

    if (!response.ok) {
      throw new Error(typeof data === 'string' ? data : data.detail || 'Request failed');
    }

    return data;
  } catch (error) {
    if (error instanceof Error) {
      throw new Error(`Failed to connect to backend at ${url}: ${error.message}`);
    }
    throw new Error(`Failed to connect to backend at ${url}`);
  }
}

export async function healthCheck() {
  try {
    return await request('/');
  } catch (error) {
    return request('/testing/health');
  }
}

/**
 * Upload the factory sensor CSV and generate analytics for one machine.
 *
 * Unlike the aircraft dataset (one row per flight), the factory dataset has
 * one row per machine, so there is no "latest reading" to pick automatically
 * beyond "first row in the file" unless a machineId is given. peerScope
 * controls whether the machine is compared against its own machine type
 * ("machine_type", the default) or the whole fleet ("fleet").
 */
export async function uploadAnalyticsFile(file, { machineId, peerScope } = {}) {
  const formData = new FormData();
  formData.append('csv_file', file);

  const params = new URLSearchParams();
  if (machineId) params.set('machine_id', machineId);
  if (peerScope) params.set('peer_scope', peerScope);
  const query = params.toString() ? `?${params.toString()}` : '';

  return request(`/factory/analytics${query}`, {
    method: 'POST',
    body: formData,
  });
}

export async function getMaintenanceRecommendation(analyticsPayload) {
  return request('/factory/maintenance-prediction', {
    method: 'POST',
    body: JSON.stringify(analyticsPayload),
  });
}
