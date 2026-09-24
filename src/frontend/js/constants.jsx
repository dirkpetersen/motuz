// The server's local filesystem is shown under the server's short host name
const hostname = (typeof window !== 'undefined' && window.location.hostname) || '';

const constants = {
    local_name: hostname.split('.')[0] || 'local',
};

export default constants;
