export function createApi(base='/api') {
  async function request(path, body) {
    const response = await fetch(base + '/' + path, body === undefined ? {} : {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(data.error || data.message || ('请求失败：' + response.status));
    return data;
  }
  return {
    request,
    status: () => request('status'),
    frames: () => request('frames'),
    scenes: () => request('scenes'),
    action: (op, body={}) => request('action/' + op, body),
    exportJob: body => request('jobs/export', body),
    job: id => request('jobs/' + encodeURIComponent(id)),
  };
}
