#define _GNU_SOURCE
/* Experimental Linux 4.1.13.26 text receive bridge. Not enabled by default. */
#include <arpa/inet.h>
#include <dlfcn.h>
#include <elf.h>
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <link.h>
#include <linux/if_alg.h>
#include <poll.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "personal_account.h"
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

static const unsigned char build_id[20]={0xd6,0x2b,0xde,0x21,0xb2,0x6c,0x59,0x28,0xe7,0xde,0xa8,0x26,0x27,0x1e,0x21,0xa5,0x42,0xb7,0x55,0xb5};
static const unsigned char expected[14]={0x55,0x41,0x57,0x41,0x56,0x41,0x55,0x41,0x54,0x53,0x48,0x83,0xec,0x48};
static const unsigned char exe_sha256[32]={0x27,0x49,0x53,0x5f,0x29,0x9b,0xa6,0x7f,0xeb,0x8e,0xf8,0x0c,0xab,0x89,0x8b,0xb0,0x53,0x1f,0xa8,0xf3,0x76,0xb1,0x6f,0x8a,0x72,0xdb,0x70,0x68,0x11,0xc8,0x41,0x32};
#define QUEUE_SIZE 128
struct event { unsigned char *fields[4]; uint32_t lengths[4]; uint64_t seq; uint64_t native_id; uint32_t type; };
typedef uintptr_t (*sync_fn)(void*,void*,void*);
static sync_fn original;
static struct event *queue[QUEUE_SIZE];static size_t qhead,qcount;
static pthread_mutex_t qlock=PTHREAD_MUTEX_INITIALIZER;static pthread_cond_t qready=PTHREAD_COND_INITIALIZER;
static pthread_t worker;static atomic_int running;static atomic_int accepting;static atomic_int installed;static atomic_uint callbacks;static atomic_ullong sequence;
static char socket_path[sizeof(((struct sockaddr_un*)0)->sun_path)];static pid_t controller_pid;static int test_only;static int stop_fd=-1;
static int panel_media,media_test,voice_test;static char media_arm[96],media_peer[128],media_self[128];static unsigned media_images,media_files,media_voices;

static int stop_requested(void){
 if(stop_fd<0)return 0;
 struct pollfd p={stop_fd,POLLIN|POLLHUP,0};int n=poll(&p,1,0);
 return n<0 ? errno!=EINTR : n>0 && (p.revents&(POLLIN|POLLHUP|POLLERR|POLLNVAL));
}
static void notice(const char *s){ssize_t ignored=write(STDERR_FILENO,s,strlen(s));(void)ignored;}
static int safe_read(void *out,const void *in,size_t n){struct iovec a={out,n},b={(void*)in,n};return process_vm_readv(getpid(),&a,1,&b,1,0)==(ssize_t)n;}
static void event_free(struct event *e){if(!e)return;for(int i=0;i<4;i++)free(e->fields[i]);free(e);}
static int get_field(const void *obj,unsigned off,unsigned char **out,uint32_t *len,uint64_t maxlen){
 uintptr_t child=0,s=0,data=0;unsigned char header=0;uint64_t n=0;
 if(!safe_read(&child,(const char*)obj+off,8)||!child||!safe_read(&s,(void*)(child+8),8)||!s||!safe_read(&header,(void*)s,1))return 0;
 if(header&1){if(!safe_read(&n,(void*)(s+8),8)||!safe_read(&data,(void*)(s+16),8))return 0;}else{n=header>>1;data=s+1;}
 if(!n||n>UINT32_MAX||n>maxlen||!data||(off!=32&&n>127))return 0;
 unsigned char *buf=malloc((size_t)n);if(!buf)return 0;
 if(!safe_read(buf,(void*)data,(size_t)n)){free(buf);return 0;}
 unsigned char *check=malloc((size_t)n);uintptr_t child2=0,s2=0;unsigned char header2=0;uint64_t n2=0;uintptr_t data2=0;
 if(!check||!safe_read(check,(void*)data,(size_t)n)||memcmp(buf,check,(size_t)n)||
    !safe_read(&child2,(const char*)obj+off,8)||child2!=child||
    !safe_read(&s2,(void*)(child+8),8)||s2!=s||!safe_read(&header2,(void*)s,1)||header2!=header){free(check);free(buf);return 0;}
 if(header&1){if(!safe_read(&n2,(void*)(s+8),8)||!safe_read(&data2,(void*)(s+16),8)||n2!=n||data2!=data){free(check);free(buf);return 0;}}
 free(check);*out=buf;*len=(uint32_t)n;return 1;
}
/* Optional new_sync field 10: preserve only a stable, bounded copy. A missing or
 * unreadable source must never discard an otherwise valid message. */
static void get_source(const void *obj,unsigned char **out,uint32_t *len){
 uintptr_t s=0,data=0,s2=0,data2=0;unsigned char h=0,h2=0;uint64_t n=0,n2=0;
 if(!safe_read(&s,(const char*)obj+0x38,8)||!s||!safe_read(&h,(void*)s,1))return;
 if(h&1){if(!safe_read(&n,(void*)(s+8),8)||!safe_read(&data,(void*)(s+16),8))return;}
 else{n=h>>1;data=s+1;}
 if(!n||n>16384||!data)return;
 unsigned char *buf=malloc((size_t)n),*check=malloc((size_t)n);
 if(!buf||!check)goto fail;
 if(!safe_read(buf,(void*)data,(size_t)n)||!safe_read(check,(void*)data,(size_t)n)||memcmp(buf,check,(size_t)n)||
    !safe_read(&s2,(const char*)obj+0x38,8)||s2!=s||!safe_read(&h2,(void*)s,1)||h2!=h)goto fail;
 if(h&1){if(!safe_read(&n2,(void*)(s+8),8)||!safe_read(&data2,(void*)(s+16),8)||n2!=n||data2!=data)goto fail;}
 free(check);*out=buf;*len=(uint32_t)n;return;
 fail: free(check);free(buf);
}
/* Exact ELF: new_sync+0x30 is SKBuiltinBuffer_t (copy 0x7df4a93).
 * Its declared size is +0x10, libc++ string pointer +8 (0x7ba64e7/4f8).
 * Copy synchronously while the protobuf remains alive; never log its bytes. */
static void get_voice(const void*obj,unsigned char**out,uint32_t*len){
 uintptr_t child=0,s=0,data=0,child2=0,s2=0,data2=0;
 uint32_t declared=0,declared2=0;uint64_t n=0,n2=0;unsigned char h=0,h2=0;
 if(!safe_read(&child,(const char*)obj+0x30,8)||!child||
    !safe_read(&declared,(void*)(child+0x10),4)||declared<=12||declared>262144||
    !safe_read(&s,(void*)(child+8),8)||!s||!safe_read(&h,(void*)s,1))return;
 if(h&1){if(!safe_read(&n,(void*)(s+8),8)||!safe_read(&data,(void*)(s+16),8))return;}
 else{n=h>>1;data=s+1;}
 if(n!=declared||!data)return;
 unsigned char *buf=malloc((size_t)n),*check=malloc((size_t)n);
 if(!buf||!check)goto fail;
 if(!safe_read(buf,(void*)data,(size_t)n)||
    (memcmp(buf,"\x02#!SILK_V3",10)&&memcmp(buf,"#!SILK_V3",9))||
    !safe_read(check,(void*)data,(size_t)n)||memcmp(buf,check,(size_t)n)||
    !safe_read(&child2,(const char*)obj+0x30,8)||child2!=child||
    !safe_read(&declared2,(void*)(child+0x10),4)||declared2!=declared||
    !safe_read(&s2,(void*)(child+8),8)||s2!=s||!safe_read(&h2,(void*)s,1)||h2!=h)goto fail;
 if(h&1){if(!safe_read(&n2,(void*)(s+8),8)||!safe_read(&data2,(void*)(s+16),8)||n2!=n||data2!=data)goto fail;}
 free(check);*out=buf;*len=(uint32_t)n;return;
 fail: free(check);free(buf);
}
static void capture(const void *obj){
 uint32_t type=0;if(!obj||!safe_read(&type,(const char*)obj+20,4))return;
 if(type!=1){
  int media=(type==3||type==49||type==34);
  int monitor_only=(type==37||type==43||type==47||type==10000||type==10002);
  if((!media&&!monitor_only)||(monitor_only&&!panel_media)||(media&&!media_test&&!panel_media)||
     (type==34&&!voice_test&&!panel_media))return;
 }
 struct event *e=calloc(1,sizeof(*e));if(!e){notice("helper-linux: event allocation failed\n");return;}
 for(int i=0;i<3;i++){
  uint64_t maxlen=(i==2&&(type==34||type==37||type==43||type==10000||type==10002))?16384:UINT32_MAX;
  if(!get_field(obj,(unsigned[]){8,24,32}[i],&e->fields[i],&e->lengths[i],maxlen)){event_free(e);return;}
 }
 e->type=type;
 unsigned char present=0,present2=0;uint64_t native_id=0,native_id2=0;
 if(safe_read(&present,(const char*)obj+0x6d,1)&&(present&8)&&safe_read(&native_id,(const char*)obj+0x50,8)&&
    safe_read(&present2,(const char*)obj+0x6d,1)&&present==present2&&
    safe_read(&native_id2,(const char*)obj+0x50,8)&&native_id==native_id2)e->native_id=native_id;
 if(type==1)get_source(obj,&e->fields[3],&e->lengths[3]);
 if(type==34)get_voice(obj,&e->fields[3],&e->lengths[3]);
 int include=type==1&&(!test_only||memmem(e->fields[2],e->lengths[2],"WX_LINUX_BRIDGE_07",18)||memmem(e->fields[2],e->lengths[2],"WX_LINUX_BRIDGE_08",18));
 pthread_mutex_lock(&qlock);
 if(panel_media&&type!=1)include=1;
 if(media_test){
  if(type==1&&e->lengths[2]==strlen(media_arm)&&!memcmp(e->fields[2],media_arm,e->lengths[2])&&
     helper_personal_account(e->fields[0],e->lengths[0])&&helper_personal_account(e->fields[1],e->lengths[1])){
   int same=!media_peer[0]||(strlen(media_peer)==e->lengths[0]&&!memcmp(media_peer,e->fields[0],e->lengths[0])&&strlen(media_self)==e->lengths[1]&&!memcmp(media_self,e->fields[1],e->lengths[1]));
   if(same){memcpy(media_peer,e->fields[0],e->lengths[0]);media_peer[e->lengths[0]]=0;memcpy(media_self,e->fields[1],e->lengths[1]);media_self[e->lengths[1]]=0;media_images=1;media_files=1;media_voices=voice_test?1:0;include=1;}
  }else if(type!=1&&media_peer[0]&&strlen(media_peer)==e->lengths[0]&&strlen(media_self)==e->lengths[1]&&
           !memcmp(media_peer,e->fields[0],e->lengths[0])&&!memcmp(media_self,e->fields[1],e->lengths[1])){
   include=(type==3&&media_images)||(type==49&&media_files)||(type==34&&media_voices);
  }
 }
 if(!include){pthread_mutex_unlock(&qlock);event_free(e);return;}
 e->seq=atomic_fetch_add(&sequence,1)+1;
 if(stop_requested() || getppid()!=controller_pid)atomic_store(&accepting,0);
 if(!atomic_load(&running)||!atomic_load(&accepting)){pthread_mutex_unlock(&qlock);event_free(e);return;}
 if(qcount==QUEUE_SIZE){pthread_mutex_unlock(&qlock);event_free(e);notice("helper-linux: receive queue full\n");return;}
 if(type==3&&media_images)media_images--;
 if(type==49&&media_files)media_files--;
  if(type==34&&media_voices)media_voices--;
 queue[(qhead+qcount)%QUEUE_SIZE]=e;qcount++;pthread_cond_signal(&qready);pthread_mutex_unlock(&qlock);
}
static uintptr_t replacement(void *a,void *b,void *c){
 atomic_fetch_add(&callbacks,1);
 if(getppid()!=controller_pid || stop_requested())atomic_store(&accepting,0);
 if(atomic_load(&running)&&atomic_load(&accepting))capture(b);
 atomic_fetch_sub(&callbacks,1);return original(a,b,c);
}
static int waitfd(int fd,short events){struct pollfd p={fd,events,0};while(atomic_load(&running)){if(stop_requested()){atomic_store(&accepting,0);return 0;}int n=poll(&p,1,100);if(n>0)return (p.revents&events)!=0;if(n<0&&errno!=EINTR)return 0;}return 0;}
static int connect_socket(void){
 int fd=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0);if(fd<0)return -1;
 struct sockaddr_un a={.sun_family=AF_UNIX};memcpy(a.sun_path,socket_path,strlen(socket_path)+1);
 if(connect(fd,(struct sockaddr*)&a,sizeof(a))<0){if(errno!=EINPROGRESS||!waitfd(fd,POLLOUT)){close(fd);return -1;}int err=0;socklen_t n=sizeof(err);if(getsockopt(fd,SOL_SOCKET,SO_ERROR,&err,&n)<0||err){close(fd);return -1;}}
 struct ucred cred;socklen_t credlen=sizeof(cred);
 if(getsockopt(fd,SOL_SOCKET,SO_PEERCRED,&cred,&credlen)<0 || cred.pid!=controller_pid || cred.uid!=geteuid()){close(fd);return -1;}
 return fd;
}
static int send_all(int fd,const void *data,size_t n){const unsigned char *p=data;while(n&&atomic_load(&running)){if(stop_requested()){atomic_store(&accepting,0);return 0;}ssize_t m=send(fd,p,n,MSG_NOSIGNAL);if(m>0){p+=m;n-=(size_t)m;}else if(m<0&&(errno==EAGAIN||errno==EWOULDBLOCK)){if(!waitfd(fd,POLLOUT))return 0;}else if(m<0&&errno==EINTR)continue;else return 0;}return n==0;}
static int send_event(int fd,const struct event *e){
 unsigned char h[40];int has_source=e->type==1&&e->lengths[3],voice=e->type==34;
 int extended=voice||e->native_id||e->type==37||e->type==43||e->type==47||e->type==10000||e->type==10002;
 size_t header_size=extended?40:e->type==1?(has_source?28:24):28;
 memcpy(h,voice?"HLV1":extended?"HLE1":e->type==1?(has_source?"HLA1":"HLT1"):"HLM1",4);uint64_t seq=htobe64(e->seq);memcpy(h+4,&seq,8);
 for(int i=0;i<3;i++){uint32_t n=htonl(e->lengths[i]);memcpy(h+12+4*i,&n,4);}
 if(extended){uint32_t type=htonl(e->type),n=htonl((has_source||voice)?e->lengths[3]:0);uint64_t id=htobe64(e->native_id);memcpy(h+24,&type,4);memcpy(h+28,&id,8);memcpy(h+36,&n,4);}
 else if(e->type!=1){uint32_t type=htonl(e->type);memcpy(h+24,&type,4);}
 else if(has_source){uint32_t n=htonl(e->lengths[3]);memcpy(h+24,&n,4);}
 if(!send_all(fd,h,header_size))return 0;
 for(int i=0;i<((has_source||voice)?4:3);i++)if(!send_all(fd,e->fields[i],e->lengths[i]))return 0;
 unsigned char ack[8];size_t have=0;
 while(have<sizeof(ack)&&atomic_load(&running)){
  ssize_t n=recv(fd,ack+have,sizeof(ack)-have,0);
  if(n>0)have+=(size_t)n;
  else if(n<0&&(errno==EAGAIN||errno==EWOULDBLOCK)){if(!waitfd(fd,POLLIN))return 0;}
  else if(n<0&&errno==EINTR)continue;
  else return 0;
 }
 if(have!=sizeof(ack))return 0;
 if(!memcmp(ack,h+4,sizeof(ack)))return 1;
 unsigned char stop[8];for(int i=0;i<8;i++)stop[i]=(unsigned char)~h[4+i];
 if(!memcmp(ack,stop,sizeof(ack))){atomic_store(&accepting,0);return 1;}
 return 0;
}
static void *run_worker(void *unused){
 (void)unused;int fd=-1;struct timespec delay={0,100000000};
 while(atomic_load(&running)&&atomic_load(&accepting)){
  if(getppid()!=controller_pid||stop_requested()){atomic_store(&accepting,0);break;}
  if(!atomic_load(&installed)){nanosleep(&delay,NULL);continue;}
  if(fd<0){
   fd=connect_socket();if(fd<0){nanosleep(&delay,NULL);continue;}
   if(!send_all(fd,"HLR1",4)){close(fd);fd=-1;nanosleep(&delay,NULL);continue;}
  }
  pthread_mutex_lock(&qlock);
  while(!qcount&&atomic_load(&running)&&atomic_load(&accepting)){
   if(stop_requested()||getppid()!=controller_pid){atomic_store(&accepting,0);break;}
   struct timespec until;clock_gettime(CLOCK_REALTIME,&until);until.tv_nsec+=200000000;
   if(until.tv_nsec>=1000000000){until.tv_sec++;until.tv_nsec-=1000000000;}
   pthread_cond_timedwait(&qready,&qlock,&until);
  }
  if(stop_requested()||getppid()!=controller_pid)atomic_store(&accepting,0);
  if(!atomic_load(&running)||!atomic_load(&accepting)){pthread_mutex_unlock(&qlock);break;}
  struct event *e=queue[qhead];qhead=(qhead+1)%QUEUE_SIZE;qcount--;pthread_mutex_unlock(&qlock);
  while(atomic_load(&running)&&atomic_load(&accepting)){
   if(getppid()!=controller_pid||stop_requested()){atomic_store(&accepting,0);break;}
   if(fd<0){
    fd=connect_socket();if(fd<0){nanosleep(&delay,NULL);continue;}
    if(!send_all(fd,"HLR1",4)){close(fd);fd=-1;nanosleep(&delay,NULL);continue;}
   }
   if(send_event(fd,e))break;
   close(fd);fd=-1;nanosleep(&delay,NULL);
  }
  event_free(e);
  if(!atomic_load(&accepting)){
   if(fd>=0){close(fd);fd=-1;}
   pthread_mutex_lock(&qlock);
   while(qcount){event_free(queue[qhead]);qhead=(qhead+1)%QUEUE_SIZE;qcount--;}
   pthread_mutex_unlock(&qlock);
  }
 }
 if(fd>=0)close(fd);
 return NULL;
}
static int check_hash(void){
 int tfm=-1,op=-1,file=-1,ok=0;unsigned char buf[65536],digest[32];
 tfm=socket(AF_ALG,SOCK_SEQPACKET|SOCK_CLOEXEC,0);if(tfm<0)goto out;
 struct sockaddr_alg sa={.salg_family=AF_ALG,.salg_type="hash",.salg_name="sha256"};
 if(bind(tfm,(struct sockaddr*)&sa,sizeof(sa))<0)goto out;
 op=accept4(tfm,NULL,NULL,SOCK_CLOEXEC);if(op<0)goto out;
 file=open("/proc/self/exe",O_RDONLY|O_CLOEXEC);if(file<0)goto out;
 for(;;){ssize_t n=read(file,buf,sizeof(buf));if(n<0){if(errno==EINTR)continue;goto out;}if(n==0)break;size_t pos=0;while(pos<(size_t)n){ssize_t k=send(op,buf+pos,(size_t)n-pos,MSG_MORE);if(k<0){if(errno==EINTR)continue;goto out;}if(!k)goto out;pos+=(size_t)k;}}
 if(send(op,NULL,0,0)<0||read(op,digest,sizeof(digest))!=(ssize_t)sizeof(digest))goto out;
 ok=memcmp(digest,exe_sha256,32)==0;
 out:if(file>=0)close(file);if(op>=0)close(op);if(tfm>=0)close(tfm);return ok;
}
static int find_build(struct dl_phdr_info *i,size_t size,void *data){
 (void)size;if(i->dlpi_name&&i->dlpi_name[0])return 0;
 for(ElfW(Half) j=0;j<i->dlpi_phnum;j++){const ElfW(Phdr)*h=&i->dlpi_phdr[j];if(h->p_type!=PT_NOTE)continue;const unsigned char*p=(const unsigned char*)(i->dlpi_addr+h->p_vaddr),*end=p+h->p_memsz;
  while(p+sizeof(ElfW(Nhdr))<=end){const ElfW(Nhdr)*n=(const ElfW(Nhdr)*)p;p+=sizeof(*n);size_t ns=(n->n_namesz+3u)&~3u,ds=(n->n_descsz+3u)&~3u;if(ns>(size_t)(end-p)||ds>(size_t)(end-p)-ns)break;if(n->n_type==NT_GNU_BUILD_ID&&n->n_namesz==4&&n->n_descsz==20&&!memcmp(p,"GNU",4)&&!memcmp(p+ns,build_id,20)){*(uintptr_t*)data=i->dlpi_addr;return 1;}p+=ns+ds;}}
 return 0;
}
static void jump(unsigned char *p,const void *to){const unsigned char code[6]={0xff,0x25,0,0,0,0};memcpy(p,code,6);memcpy(p+6,&to,8);}
__attribute__((constructor)) static void initialize(void){
 const char *path=getenv("HELPER_LINUX_EVENT_SOCKET");if(!path||path[0]!='/'||strnlen(path,sizeof(socket_path))>=sizeof(socket_path))return;
 memcpy(socket_path,path,strlen(path)+1);
 const char *pidstr=getenv("HELPER_LINUX_CONTROLLER_PID");char *end=NULL;
 if(!pidstr||!*pidstr)return;
 errno=0;long controller=strtol(pidstr,&end,10);
 if(errno||!end||*end||controller<=0||controller>INT32_MAX)return;
 controller_pid=(pid_t)controller;if(getppid()!=controller_pid)return;
 const char *stopstr=getenv("HELPER_LINUX_STOP_FD");
 if(stopstr){
  char *stopend=NULL;errno=0;long candidate=strtol(stopstr,&stopend,10);
  if(errno||stopend==stopstr||*stopend||candidate<3||candidate>INT32_MAX)return;
  struct stat st;int flags=fcntl((int)candidate,F_GETFL);
  if(flags<0||(flags&O_ACCMODE)!=O_RDONLY||fstat((int)candidate,&st)||!S_ISFIFO(st.st_mode))return;
  if(fcntl((int)candidate,F_SETFD,FD_CLOEXEC)<0)return;
  stop_fd=(int)candidate;
 }
 const char *test=getenv("HELPER_LINUX_TEST_MARKERS");test_only=test && !strcmp(test,"1");
 const char *panel=getenv("HELPER_LINUX_PANEL_MEDIA");panel_media=panel&&!strcmp(panel,"1");
 const char *voice=getenv("HELPER_LINUX_TEST_VOICE");voice_test=voice&&!strcmp(voice,"1");
  const char *mt=getenv("HELPER_LINUX_TEST_MEDIA"),*arm=getenv("HELPER_LINUX_MEDIA_ARM_TEXT");
 if(mt&&!strcmp(mt,"1")&&arm&&strlen(arm)>0&&strlen(arm)<sizeof(media_arm)){media_test=1;strcpy(media_arm,arm);}

#ifdef HELPER_FIXTURE_TEST
 unsigned char *target=(unsigned char*)dlsym(RTLD_DEFAULT,"fixture_hook_target");if(!target)return;target+=4;
#else
 if(!check_hash())return;
 uintptr_t base=0;dl_iterate_phdr(find_build,&base);if(!base)return;
 unsigned char *target=(unsigned char*)(base+0x622ea60);
#endif
 if(memcmp(target,expected,14))return;
 unsigned char *tramp=mmap(NULL,28,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);if(tramp==MAP_FAILED)return;
 memcpy(tramp,expected,14);jump(tramp+14,target+14);if(mprotect(tramp,28,PROT_READ|PROT_EXEC)){munmap(tramp,28);return;}original=(sync_fn)tramp;
 long ps=sysconf(_SC_PAGESIZE);if(ps<=0){munmap(tramp,28);return;}
 uintptr_t page=(uintptr_t)target&~((uintptr_t)ps-1);size_t len=((uintptr_t)target+14-page+(size_t)ps-1)&~((size_t)ps-1);
 atomic_store(&accepting,1);atomic_store(&running,1);if(pthread_create(&worker,NULL,run_worker,NULL)){atomic_store(&running,0);munmap(tramp,28);return;}
 if(mprotect((void*)page,len,PROT_READ|PROT_WRITE|PROT_EXEC)){atomic_store(&running,0);pthread_cond_signal(&qready);pthread_join(worker,NULL);munmap(tramp,28);return;}
 jump(target,(void*)replacement);__builtin___clear_cache((char*)target,(char*)target+14);
#ifdef HELPER_FIXTURE_FAIL_RESTORE
 int restore_result=-1;
#else
 int restore_result=mprotect((void*)page,len,PROT_READ|PROT_EXEC);
#endif
 if(restore_result){
  /* 不要因桥接初始化失败而终止用户微信；还原入口并拒绝就绪。 */
  memcpy(target,expected,14);__builtin___clear_cache((char*)target,(char*)target+14);
  if(mprotect((void*)page,len,PROT_READ|PROT_EXEC))
   notice("helper-linux: failed to restore code page permissions\n");
  atomic_store(&accepting,0);atomic_store(&running,0);pthread_cond_signal(&qready);
  pthread_join(worker,NULL);munmap(tramp,28);notice("helper-linux: bridge initialization failed\n");return;
 }
 atomic_store(&installed,1);
 notice("helper-linux: experimental receive bridge loaded\n");
}
__attribute__((destructor)) static void shutdown_bridge(void){
 if(!atomic_exchange(&running,0))return;
 pthread_mutex_lock(&qlock);pthread_cond_broadcast(&qready);pthread_mutex_unlock(&qlock);pthread_join(worker,NULL);
 struct timespec wait={0,1000000};for(int i=0;i<100&&atomic_load(&callbacks);i++)nanosleep(&wait,NULL);
 if(!atomic_load(&callbacks)){pthread_mutex_lock(&qlock);while(qcount){event_free(queue[qhead]);qhead=(qhead+1)%QUEUE_SIZE;qcount--;}pthread_mutex_unlock(&qlock);}
}
