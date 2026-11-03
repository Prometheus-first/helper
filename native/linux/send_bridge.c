#define _GNU_SOURCE
/* Experimental Linux native text sender. Commands are serialized; no automatic retries. */
#include <dlfcn.h>
#include <elf.h>
#include <link.h>
#include <linux/if_alg.h>
#include <sys/socket.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/uio.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <unistd.h>
#include <arpa/inet.h>

#include <pthread.h>
#include <sys/un.h>
#include <time.h>
#include <stddef.h>
#include <sys/time.h>
#include <sys/mman.h>
#include <inttypes.h>

static const unsigned char build_id[20]={0xd6,0x2b,0xde,0x21,0xb2,0x6c,0x59,0x28,0xe7,0xde,0xa8,0x26,0x27,0x1e,0x21,0xa5,0x42,0xb7,0x55,0xb5};
static const unsigned char exe_sha256[32]={0x27,0x49,0x53,0x5f,0x29,0x9b,0xa6,0x7f,0xeb,0x8e,0xf8,0x0c,0xab,0x89,0x8b,0xb0,0x53,0x1f,0xa8,0xf3,0x76,0xb1,0x6f,0x8a,0x72,0xdb,0x70,0x68,0x11,0xc8,0x41,0x32};
static int rd(void*out,const void*in,size_t n){struct iovec a={out,n},b={(void*)in,n};return process_vm_readv(getpid(),&a,1,&b,1,0)==(ssize_t)n;}
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
static int main_build(struct dl_phdr_info *info,size_t size,void *data){
 (void)size;if(info->dlpi_name&&info->dlpi_name[0])return 0;
 for(ElfW(Half) i=0;i<info->dlpi_phnum;i++){const ElfW(Phdr)*h=&info->dlpi_phdr[i];if(h->p_type!=PT_NOTE)continue;const unsigned char*p=(const unsigned char*)(info->dlpi_addr+h->p_vaddr),*end=p+h->p_memsz;
  while(p+sizeof(ElfW(Nhdr))<=end){const ElfW(Nhdr)*n=(const ElfW(Nhdr)*)p;p+=sizeof(*n);size_t names=(n->n_namesz+3u)&~3u,desc=(n->n_descsz+3u)&~3u;if(names>(size_t)(end-p)||desc>(size_t)(end-p)-names)break;if(n->n_type==NT_GNU_BUILD_ID&&n->n_namesz==4&&n->n_descsz==20&&!memcmp(p,"GNU",4)&&!memcmp(p+names,build_id,20)){*(uintptr_t*)data=info->dlpi_addr;return 1;}p+=names+desc;}}
 return 0;
}

struct sdk_string { unsigned char bytes[24]; };
struct sdk_callback { atomic_int refs; uint32_t pad; void (*invoke)(struct sdk_callback*,const struct sdk_string*,const struct sdk_string*); void (*destroy)(struct sdk_callback*); int (*check)(void*,int); };
_Static_assert(offsetof(struct sdk_callback,invoke)==8,"invoke ABI");
_Static_assert(offsetof(struct sdk_callback,destroy)==16,"destroy ABI");
_Static_assert(sizeof(struct sdk_string)==24,"string ABI");
typedef void (*sdk_entry)(void*,const struct sdk_string*,const struct sdk_string*,struct sdk_callback**);
typedef void (*sdk_ctor)(struct sdk_string*,const char*,size_t);
static sdk_entry sdk_send,sdk_send_generic;static sdk_ctor sdk_str;static void (*sdk_delete)(void*);
static atomic_int sdk_context_verified;
static int sdk_listener=-1;static pid_t sdk_parent;
static pthread_mutex_t sdk_mutex=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t sdk_cond=PTHREAD_COND_INITIALIZER;
static int sdk_done,sdk_running;static char sdk_result[8192],sdk_error[2048],sdk_path[108];
static size_t sdk_copy(char*out,size_t cap,const struct sdk_string*s){
 unsigned char h[24];if(!s||!rd(h,s,sizeof(h)))return 0;
 size_t n;uintptr_t p;if(h[0]&1){memcpy(&n,h+8,8);memcpy(&p,h+16,8);}else{n=h[0]>>1;p=(uintptr_t)s+1;if(n>22)return 0;}
 if(n>=cap||!cap||!rd(out,(void*)p,n))return 0;
 out[n]=0;return n;
}
static void sdk_cb_destroy(struct sdk_callback*c){free(c);}
static int sdk_cb_check(void*x,int v){(void)x;return v!=0;}
static void sdk_cb_invoke(struct sdk_callback*c,const struct sdk_string*a,const struct sdk_string*b){
 (void)c;pthread_mutex_lock(&sdk_mutex);
 sdk_copy(sdk_result,sizeof(sdk_result),a);sdk_copy(sdk_error,sizeof(sdk_error),b);sdk_done=1;
 pthread_cond_broadcast(&sdk_cond);pthread_mutex_unlock(&sdk_mutex);
}
static void sdk_string_destroy(struct sdk_string*s){if(s->bytes[0]&1){void*p=NULL;memcpy(&p,s->bytes+16,8);sdk_delete(p);}memset(s,0,sizeof(*s));}
static void sdk_write(int fd,const char*s){size_t n=strlen(s);while(n){ssize_t k=send(fd,s,n,MSG_NOSIGNAL);if(k<0){if(errno==EINTR)continue;break;}if(!k)break;s+=k;n-=k;}}
static void sdk_escape(char*out,size_t cap,const char*s){size_t n=0;for(;*s&&n+7<cap;s++){unsigned char c=(unsigned char)*s;if(c=='"'||c=='\\'){out[n++]='\\';out[n++]=(char)c;}else if(c<32){int k=snprintf(out+n,cap-n,"\\u%04x",c);n+=(size_t)k;}else out[n++]=(char)c;}out[n]=0;}
struct sdk_function { _Alignas(16) unsigned char storage[32]; void *callable; };
struct sdk_payload { atomic_uint refs; char operation; char *json; };
struct sdk_callable { void **vtable; struct sdk_payload *payload; };
struct sdk_source { const char *file; const char *function; uint32_t line,pad; const void *pc; };
struct sdk_shared { void *object; void *control; };
#ifndef SDK_FIXTURE
static uintptr_t sdk_base;
#endif
static void sdk_finish_error(const char*msg){
 pthread_mutex_lock(&sdk_mutex);snprintf(sdk_error,sizeof(sdk_error),"%s",msg);sdk_result[0]=0;sdk_done=1;pthread_cond_broadcast(&sdk_cond);pthread_mutex_unlock(&sdk_mutex);
}
static int sdk_has_coroutine(void){
#ifdef SDK_FIXTURE
 int(*check)(void)=(int(*)(void))dlsym(RTLD_DEFAULT,"fixture_sdk_context");return check&&check();
#else
 uintptr_t thread=(uintptr_t)__builtin_thread_pointer(),ptr=0,co=0;
 return rd(&ptr,(void*)(thread-0x78),8)&&ptr&&rd(&co,(void*)ptr,8)&&co;
#endif
}
static void sdk_release(struct sdk_shared*s);
static void sdk_finish_success(void){pthread_mutex_lock(&sdk_mutex);strcpy(sdk_result,"{\"success\":true}");sdk_error[0]=0;sdk_done=1;pthread_cond_broadcast(&sdk_cond);pthread_mutex_unlock(&sdk_mutex);}
struct native_media_status {uint32_t error_type,error_code;struct sdk_string error;void *object,*control;};
_Static_assert(sizeof(struct native_media_status)==48,"send result ABI");
static int unhex(char c){if(c>='0'&&c<='9')return c-'0';if(c>='a'&&c<='f')return c-'a'+10;return -1;}
static void sdk_perform_media(char operation,const char*payload){
 const char*sep=strchr(payload,'\n');if(!sep||sep==payload||sep-payload>127){sdk_finish_error("invalid media target");return;}
 size_t tn=(size_t)(sep-payload),hexlen=strlen(sep+1);if(!hexlen||(hexlen&1)){sdk_finish_error("invalid media path frame");return;}
 char target[128];memcpy(target,payload,tn);target[tn]=0;
 for(size_t i=0;i<tn;i++)if(!((target[i]>='a'&&target[i]<='z')||(target[i]>='A'&&target[i]<='Z')||(target[i]>='0'&&target[i]<='9')||strchr("_.@-",target[i]))){sdk_finish_error("invalid media target");return;}
 char*path=malloc(hexlen/2+1);if(!path){sdk_finish_error("allocation failed");return;}
 for(size_t i=0;i<hexlen;i+=2){int a=unhex(sep[1+i]),b=unhex(sep[2+i]);if(a<0||b<0||(!a&&!b)){free(path);sdk_finish_error("invalid media path encoding");return;}path[i/2]=(char)((a<<4)|b);}path[hexlen/2]=0;
 struct stat st;if(path[0]!='/'||stat(path,&st)||!S_ISREG(st.st_mode)||st.st_size<=0){free(path);sdk_finish_error("media file missing or empty");return;}
#ifdef SDK_FIXTURE
 int(*send)(char,const char*,const char*)=(void*)dlsym(RTLD_DEFAULT,"fixture_sdk_media");
 int result=send?send(operation,target,path):-1;free(path);
 if(result==1)sdk_finish_success();else sdk_finish_error("fixture media send rejected");
#else
 struct sdk_string to={0};unsigned char native_path[24]={0};
 struct sdk_shared hub={0},center={0},manager={0},message={0};struct native_media_status status={0};
 sdk_str(&to,target,tn);
 ((void(*)(void*,const char*,size_t))(sdk_base+0x453c040))(native_path,path,hexlen/2);free(path);
 ((void(*)(struct sdk_shared*))(sdk_base+0x60421e0))(&hub);
 if(hub.object)((void(*)(struct sdk_shared*,void*))(sdk_base+0x619ce90))(&center,hub.object);
 if(center.object)((void(*)(struct sdk_shared*,void*))(sdk_base+0x61ad930))(&manager,center.object);
 int invoked=0;
 if(manager.object){
  unsigned char scratch[64]={0};
  ((void(*)(struct sdk_shared*,void*))(sdk_base+(operation=='I'?0x4a03f10:0x5325050)))(&message,scratch);
  if(message.object){
   uint32_t type=operation=='I'?3:49,subtype=operation=='I'?0:6;
   memcpy((char*)message.object+0xe4,&type,4);memcpy((char*)message.object+0xe8,&subtype,4);
   ((void(*)(void*,const struct sdk_string*))(sdk_base+0x450fdb0))((char*)message.object+0x90,&to);
   ((void(*)(void*,const void*))(sdk_base+0x45396a0))((char*)message.object+0xf0,native_path);
   ((void(*)(struct native_media_status*,void*,struct sdk_shared*))(sdk_base+0x64ccca0))(&status,manager.object,&message);invoked=1;
  }
 }
 if(invoked&&!status.error_type&&!status.error_code)sdk_finish_success();
 else if(invoked){char error[1024]={0};sdk_copy(error,sizeof(error),&status.error);sdk_finish_error(error[0]?error:"native media send failed");}
 else sdk_finish_error("native message manager unavailable");
 if(invoked)((void(*)(struct native_media_status*))(sdk_base+0x64cdaa0))(&status);
 sdk_release(&message);sdk_release(&manager);sdk_release(&center);sdk_release(&hub);
 ((void(*)(void*))(sdk_base+0x4539690))(native_path);sdk_string_destroy(&to);
#endif
}
/* Retain the exact message context exported by the native DTO constructor. */
typedef void*(*sdk_image_dto_fn)(void*,void*);
static sdk_image_dto_fn sdk_original_image_dto;
static int sdk_download_hook_ready;
static atomic_int sdk_image_capture_enabled;
static atomic_uint sdk_image_scoped_seen,sdk_image_cached_total;
static pthread_mutex_t sdk_image_mutex=PTHREAD_MUTEX_INITIALIZER;
static char sdk_image_peer[128],sdk_image_account[128];
struct sdk_image_context {char md5[33];struct sdk_shared value;};
static struct sdk_image_context sdk_images[16];static unsigned sdk_image_next;
static int sdk_md5_valid(const char*value){if(strlen(value)!=32)return 0;for(int i=0;i<32;i++)if(unhex(value[i])<0)return 0;return 1;}
static void sdk_shared_retain(struct sdk_shared*value){if(value->control)__atomic_add_fetch((int64_t*)((char*)value->control+8),1,__ATOMIC_SEQ_CST);}
static void*sdk_capture_image_dto(void*output,void*source){
 void*result=sdk_original_image_dto(output,source);
 uint32_t type=0;if(!atomic_load(&sdk_image_capture_enabled)||!rd(&type,(char*)output+8,4)||type!=3)return result;
 char peer[128],account[128],md5[33];
 if(!sdk_copy(peer,sizeof(peer),(struct sdk_string*)((char*)source+0x18))||!sdk_copy(account,sizeof(account),(struct sdk_string*)((char*)source+0x30)))return result;
 pthread_mutex_lock(&sdk_image_mutex);int scoped=!strcmp(peer,sdk_image_peer)&&!strcmp(account,sdk_image_account);pthread_mutex_unlock(&sdk_image_mutex);
 if(!scoped)return result;
 atomic_fetch_add(&sdk_image_scoped_seen,1);
 void*metadata=NULL;struct sdk_shared context={0};
 if(!rd(&metadata,(char*)source+0x220,8)||!metadata||sdk_copy(md5,sizeof(md5),(struct sdk_string*)((char*)metadata+0x1c8))!=32||!sdk_md5_valid(md5)||!rd(&context,(char*)output+0x238,sizeof(context))||!context.object||!context.control)return result;
 sdk_shared_retain(&context);struct sdk_shared previous={0};
 pthread_mutex_lock(&sdk_image_mutex);
 if(!atomic_load(&sdk_image_capture_enabled)||strcmp(peer,sdk_image_peer)||strcmp(account,sdk_image_account)){pthread_mutex_unlock(&sdk_image_mutex);sdk_release(&context);return result;}
 unsigned slot=sdk_image_next++%16;for(unsigned i=0;i<16;i++)if(!strcmp(sdk_images[i].md5,md5)){slot=i;break;}
 previous=sdk_images[slot].value;strcpy(sdk_images[slot].md5,md5);sdk_images[slot].value=context;atomic_fetch_add(&sdk_image_cached_total,1);
 pthread_mutex_unlock(&sdk_image_mutex);sdk_release(&previous);return result;
}
static void sdk_absolute_jump(unsigned char*out,const void*target){out[0]=0xff;out[1]=0x25;memset(out+2,0,4);memcpy(out+6,&target,8);}
static int sdk_install_image_context_capture(void){
 unsigned char*entry;
#ifdef SDK_FIXTURE
 entry=(unsigned char*)dlsym(RTLD_DEFAULT,"fixture_image_dto");if(!entry)return 0;
 static const unsigned char endbr[4]={0xf3,0x0f,0x1e,0xfa};if(!memcmp(entry,endbr,4))entry+=4;
 for(int i=0;i<15;i++)if(entry[i]!=0x90)return 0;
#else
 entry=(unsigned char*)(sdk_base+0x605e8a0);
 static const unsigned char expected[15]={0x55,0x41,0x57,0x41,0x56,0x41,0x54,0x53,0x48,0x83,0xec,0x30,0x49,0x89,0xf6};
 if(memcmp(entry,expected,15))return 0;
#endif
 long page=sysconf(_SC_PAGESIZE);if(page<=0)return 0;
 unsigned char*trampoline=mmap(NULL,(size_t)page,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);if(trampoline==MAP_FAILED)return 0;
 unsigned char original[15];memcpy(original,entry,15);memcpy(trampoline,entry,15);sdk_absolute_jump(trampoline+15,entry+15);
 if(mprotect(trampoline,(size_t)page,PROT_READ|PROT_EXEC)){munmap(trampoline,(size_t)page);return 0;}
 uintptr_t start=(uintptr_t)entry&~((uintptr_t)page-1),end=((uintptr_t)entry+15+(uintptr_t)page-1)&~((uintptr_t)page-1);
 if(mprotect((void*)start,end-start,PROT_READ|PROT_WRITE|PROT_EXEC)){munmap(trampoline,(size_t)page);return 0;}
 sdk_original_image_dto=(sdk_image_dto_fn)trampoline;
 unsigned char patch[15];sdk_absolute_jump(patch,sdk_capture_image_dto);patch[14]=0x90;memcpy(entry,patch,sizeof(patch));__builtin___clear_cache((char*)entry,(char*)entry+15);
 if(mprotect((void*)start,end-start,PROT_READ|PROT_EXEC)){
  memcpy(entry,original,15);__builtin___clear_cache((char*)entry,(char*)entry+15);mprotect((void*)start,end-start,PROT_READ|PROT_EXEC);munmap(trampoline,(size_t)page);sdk_original_image_dto=NULL;return 0;
 }
 return 1; /* Mapped for the process lifetime; scope remains disabled until A. */
}
static int sdk_get_image_context(const char*md5,struct sdk_shared*out){
 int found=0;pthread_mutex_lock(&sdk_image_mutex);
 for(unsigned i=0;i<16;i++)if(!strcmp(sdk_images[i].md5,md5)&&sdk_images[i].value.object){found=1;if(out){*out=sdk_images[i].value;sdk_shared_retain(out);}break;}
 pthread_mutex_unlock(&sdk_image_mutex);return found;
}
static void sdk_perform_image_scope(const char*payload){
 if(!sdk_download_hook_ready){sdk_finish_error("native image context capture unavailable");return;}
 const char*separator=strchr(payload,'\n');if(!separator||separator==payload||separator-payload>127||strlen(separator+1)>127||strchr(separator+1,'\n')){sdk_finish_error("invalid image scope");return;}
 char peer[128],account[128];memcpy(peer,payload,(size_t)(separator-payload));peer[separator-payload]=0;strcpy(account,separator+1);
 if(strncmp(peer,"wxid_",5)||strncmp(account,"wxid_",5)){sdk_finish_error("image scope requires account identifiers");return;}
 struct sdk_shared old[16];pthread_mutex_lock(&sdk_image_mutex);atomic_store(&sdk_image_capture_enabled,0);
 for(unsigned i=0;i<16;i++){old[i]=sdk_images[i].value;memset(&sdk_images[i],0,sizeof(sdk_images[i]));}
 strcpy(sdk_image_peer,peer);strcpy(sdk_image_account,account);sdk_image_next=0;atomic_store(&sdk_image_scoped_seen,0);atomic_store(&sdk_image_cached_total,0);atomic_store(&sdk_image_capture_enabled,1);pthread_mutex_unlock(&sdk_image_mutex);
 for(unsigned i=0;i<16;i++)sdk_release(&old[i]);
#ifdef SDK_FIXTURE
 void(*ready)(const char*,const char*)=(void*)dlsym(RTLD_DEFAULT,"fixture_image_scope_ready");if(ready)ready(peer,account);
#endif
 sdk_finish_success();
}
struct sdk_image_options {uint32_t mode,padding;unsigned char path_a[24],path_b[24];};
_Static_assert(sizeof(struct sdk_image_options)==56,"native image options ABI");
static void sdk_perform_message_download(const char*md5){
 if(!sdk_download_hook_ready||!sdk_md5_valid(md5)){sdk_finish_error("native image context capture unavailable or invalid checksum");return;}
 struct sdk_shared image={0};if(!sdk_get_image_context(md5,&image)){sdk_finish_error("native image context not ready");return;}
#ifdef SDK_FIXTURE
 int(*download)(const struct sdk_shared*,int,int,const void*)=(void*)dlsym(RTLD_DEFAULT,"fixture_native_image_download");
 struct sdk_image_options options={.mode=1};int ok=download&&download(&image,3,100,&options)==1;sdk_release(&image);
 if(!ok){sdk_finish_error("fixture native image download rejected");return;}
#else
 struct sdk_shared hub={0},center={0},manager={0},future={0};struct sdk_image_options options={.mode=1};
 ((void(*)(void*))(sdk_base+0x4539510))(options.path_a);((void(*)(void*))(sdk_base+0x4539510))(options.path_b);
 ((void(*)(struct sdk_shared*))(sdk_base+0x60421e0))(&hub);
 if(hub.object)((void(*)(struct sdk_shared*,void*))(sdk_base+0x619ce90))(&center,hub.object);
 if(center.object)((void(*)(struct sdk_shared*,void*))(sdk_base+0x61ad930))(&manager,center.object);
 int ok=0;uint32_t errors[2]={0};
 if(manager.object){
  ((void(*)(struct sdk_shared*,void*,struct sdk_shared*,int,int,struct sdk_image_options*))(sdk_base+0x6517dc0))(&future,manager.object,&image,3,100,&options);
  if(future.object){
   ((void(*)(struct sdk_shared*))(sdk_base+0x9aef560))(&future);
   void*status=((void*(*)(struct sdk_shared*))(sdk_base+0x6545070))(&future);
   if(status&&rd(errors,status,8)&&!errors[0]&&!errors[1])ok=1;
  }
 }
 sdk_release(&future);sdk_release(&manager);sdk_release(&center);sdk_release(&hub);sdk_release(&image);
 ((void(*)(void*))(sdk_base+0x4539690))(options.path_b);((void(*)(void*))(sdk_base+0x4539690))(options.path_a);
 if(!ok){char error[160];snprintf(error,sizeof(error),"native message image download failed: type=%u code=%d",errors[0],(int32_t)errors[1]);sdk_finish_error(error);return;}
#endif
 pthread_mutex_lock(&sdk_mutex);strcpy(sdk_result,"{\"success\":true,\"native_completed\":true}");sdk_error[0]=0;sdk_done=1;pthread_cond_broadcast(&sdk_cond);pthread_mutex_unlock(&sdk_mutex);
}
static void sdk_perform(char operation,const char *json_body){
 if(operation!='V'&&!sdk_has_coroutine()){sdk_finish_error("native coroutine context unavailable; no send attempted");return;}
 if(operation=='A'){sdk_perform_image_scope(json_body);return;}
 if(operation=='K'){char result[200];int available=sdk_md5_valid(json_body)&&sdk_get_image_context(json_body,NULL);snprintf(result,sizeof(result),"{\"success\":true,\"available\":%s,\"scoped_seen\":%u,\"cached\":%u}",available?"true":"false",atomic_load(&sdk_image_scoped_seen),atomic_load(&sdk_image_cached_total));pthread_mutex_lock(&sdk_mutex);strcpy(sdk_result,result);sdk_error[0]=0;sdk_done=1;pthread_cond_broadcast(&sdk_cond);pthread_mutex_unlock(&sdk_mutex);return;}
 if(operation=='D'){sdk_perform_message_download(json_body);return;}
 if(operation=='I'||operation=='F'){sdk_perform_media(operation,json_body);return;}
 if(operation=='N'){
  atomic_store(&sdk_context_verified,1);pthread_mutex_lock(&sdk_mutex);strcpy(sdk_result,"{\"success\":true,\"native_coroutine\":true}");sdk_error[0]=0;sdk_done=1;pthread_cond_broadcast(&sdk_cond);pthread_mutex_unlock(&sdk_mutex);return;
 }
 struct sdk_string json={0},request={0};
 const char*body=operation=='V'?"{}":json_body;
 sdk_str(&json,body,strlen(body));sdk_str(&request,"helper-linux-test",17);
 struct sdk_callback*cb=calloc(1,sizeof(*cb));
 if(!cb){sdk_string_destroy(&json);sdk_string_destroy(&request);sdk_finish_error("allocation failed");return;}
 atomic_init(&cb->refs,1);cb->invoke=sdk_cb_invoke;cb->destroy=sdk_cb_destroy;cb->check=sdk_cb_check;
 (operation=='G'?sdk_send_generic:sdk_send)(NULL,&json,&request,&cb);
 if(cb&&atomic_fetch_sub(&cb->refs,1)==1)cb->destroy(cb);
 sdk_string_destroy(&json);sdk_string_destroy(&request);
}
static void sdk_task_destroy(struct sdk_callable*t){if(t->payload&&atomic_fetch_sub(&t->payload->refs,1)==1){free(t->payload->json);free(t->payload);}t->payload=NULL;}
static void sdk_task_delete(struct sdk_callable*t){sdk_task_destroy(t);free(t);}
static struct sdk_callable*sdk_task_clone(struct sdk_callable*t){struct sdk_callable*n=malloc(sizeof(*n));if(n){*n=*t;if(n->payload)atomic_fetch_add(&n->payload->refs,1);}return n;}
static void sdk_task_clone_into(struct sdk_callable*t,void*dst){memcpy(dst,t,sizeof(*t));if(t->payload)atomic_fetch_add(&t->payload->refs,1);}
static void sdk_task_invoke(struct sdk_callable*t){sdk_perform(t->payload->operation,t->payload->json);}
static void*sdk_task_target(struct sdk_callable*t,const void*type){(void)t;(void)type;return NULL;}
static const void*sdk_task_type(struct sdk_callable*t){(void)t;return NULL;}
static void*sdk_task_vtable[]={sdk_task_destroy,sdk_task_delete,sdk_task_clone,sdk_task_clone_into,sdk_task_destroy,sdk_task_delete,sdk_task_invoke,sdk_task_target,sdk_task_type};
static void sdk_release(struct sdk_shared*s){
 if(s->control){int64_t*ref=(int64_t*)((char*)s->control+8);if(__atomic_fetch_sub(ref,1,__ATOMIC_SEQ_CST)==0){void**v=*(void***)s->control;((void(*)(void*))v[2])(s->control);
#ifdef SDK_FIXTURE
 void(*weak)(void*)=(void*)dlsym(RTLD_DEFAULT,"fixture_sdk_weak_release");if(weak)weak(s->control);
#else
 ((void(*)(void*))(sdk_base+0x45391d0))(s->control);
#endif
}}memset(s,0,sizeof(*s));
}
static void sdk_schedule(char operation,const char *json){
 struct sdk_callable*t=calloc(1,sizeof(*t));if(!t){sdk_finish_error("allocation failed");return;}t->vtable=sdk_task_vtable;t->payload=calloc(1,sizeof(*t->payload));
 if(!t->payload){free(t);sdk_finish_error("allocation failed");return;}
 atomic_init(&t->payload->refs,1);t->payload->operation=operation;t->payload->json=strdup(json?json:"{}");
 if(!t->payload->json){free(t->payload);free(t);sdk_finish_error("allocation failed");return;}
 struct sdk_function fn={0};fn.callable=t;
 struct sdk_source source={"helper_linux_native.c","sdk_schedule",1,0,sdk_schedule};
 struct sdk_shared executor={0},job={0};(void)executor;
#ifdef SDK_FIXTURE
 void(*dispatch)(struct sdk_shared*,void*,const struct sdk_source*,struct sdk_function*,int)=(void*)dlsym(RTLD_DEFAULT,"fixture_sdk_dispatch");
 if(!dispatch){sdk_task_delete(t);sdk_finish_error("fixture dispatcher missing");return;}dispatch(&job,NULL,&source,&fn,1);
#else
 void*app=((void*(*)(void))(sdk_base+0x6041320))();
 if(!app){sdk_task_delete(t);sdk_finish_error("native app unavailable");return;}
 ((void(*)(struct sdk_shared*,void*))(sdk_base+0x60422b0))(&executor,app);
 void*queue=NULL;if(!executor.object||!rd(&queue,(char*)executor.object+0x10,8)||!queue){sdk_task_delete(t);sdk_release(&executor);sdk_finish_error("native dispatcher unavailable");return;}
 ((void(*)(struct sdk_shared*,void*,const struct sdk_source*,struct sdk_function*,int))(sdk_base+0x6247520))(&job,executor.object,&source,&fn,1);
 sdk_release(&job);sdk_release(&executor);
#endif
 if(fn.callable){struct sdk_callable*c=fn.callable;((void(*)(void*))c->vtable[5])(c);}
}
static void sdk_handle(int client,char operation,const char *json){
 if(operation!='V'&&operation!='S'&&operation!='N'&&operation!='G'&&operation!='I'&&operation!='F'&&operation!='D'&&operation!='A'&&operation!='K'){sdk_write(client,"{\"error\":\"unsupported command\"}\n");return;}
 if((operation=='S'||operation=='G'||operation=='I'||operation=='F'||operation=='D')&&!atomic_load(&sdk_context_verified)){sdk_write(client,"{\"error\":\"native context has not passed the dry run\"}\n");return;}
 pthread_mutex_lock(&sdk_mutex);
 if(sdk_running){pthread_mutex_unlock(&sdk_mutex);sdk_write(client,"{\"error\":\"previous callback pending\"}\n");return;}
 sdk_done=0;sdk_running=1;sdk_result[0]=0;sdk_error[0]=0;pthread_mutex_unlock(&sdk_mutex);
 sdk_schedule(operation,json);
 pthread_mutex_lock(&sdk_mutex);
 struct timespec until;clock_gettime(CLOCK_REALTIME,&until);until.tv_sec+=45;
 while(!sdk_done){int rc=pthread_cond_timedwait(&sdk_cond,&sdk_mutex,&until);if(rc==ETIMEDOUT)break;}
 char result[50000];if(sdk_done){char a[32768],b[8192];sdk_escape(a,sizeof(a),sdk_result);sdk_escape(b,sizeof(b),sdk_error);snprintf(result,sizeof(result),"{\"callback\":true,\"result\":\"%s\",\"error\":\"%s\"}\n",a,b);sdk_running=0;}else snprintf(result,sizeof(result),"{\"callback\":false,\"pending\":true}\n");
 pthread_mutex_unlock(&sdk_mutex);sdk_write(client,result);
}
static int sdk_read(int fd,void*out,size_t n){unsigned char*p=out;while(n){ssize_t k=recv(fd,p,n,0);if(k<=0){if(k<0&&errno==EINTR)continue;return 0;}p+=k;n-=(size_t)k;}return 1;}
static void*sdk_serve(void*unused){
 (void)unused;
 while(getppid()==sdk_parent){
  fd_set set;FD_ZERO(&set);FD_SET(sdk_listener,&set);struct timeval tv={1,0};
  int rc=select(sdk_listener+1,&set,NULL,NULL,&tv);if(rc<0){if(errno==EINTR)continue;break;}if(!rc)continue;
  int c=accept4(sdk_listener,NULL,NULL,SOCK_CLOEXEC);if(c<0)continue;
  struct ucred peer={0};socklen_t size=sizeof(peer);
  if(getsockopt(c,SOL_SOCKET,SO_PEERCRED,&peer,&size)||peer.pid!=sdk_parent||peer.uid!=geteuid()){close(c);continue;}
  struct timeval timeout={5,0};setsockopt(c,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));
  char op=0;if(recv(c,&op,1,0)==1){
   if(op=='Q'){atomic_store(&sdk_image_capture_enabled,0);sdk_write(c,"{\"stopped\":true}\n");close(c);break;}
   if(op=='H'){char ready[180];snprintf(ready,sizeof(ready),"{\"ready\":true,\"protocol\":1,\"generic_messages\":true,\"local_media\":true,\"image_download\":%s,\"image_context\":%s}\n",sdk_download_hook_ready?"true":"false",sdk_download_hook_ready?"true":"false");sdk_write(c,ready);}
   else if(op=='S'||op=='G'||op=='I'||op=='F'||op=='D'||op=='A'||op=='K'){
    uint32_t net=0;char*body=NULL;
    if(sdk_read(c,&net,4)){size_t n=ntohl(net);if(n){body=malloc(n+1);if(body&&sdk_read(c,body,n)){body[n]=0;if(!memchr(body,0,n))sdk_handle(c,op,body);else sdk_write(c,"{\"error\":\"NUL in JSON request\"}\n");}}}
    if(!body)sdk_write(c,"{\"error\":\"invalid request frame\"}\n");
    free(body);
   }else sdk_handle(c,op,NULL);
  }close(c);
 }
 atomic_store(&sdk_image_capture_enabled,0);
 close(sdk_listener);sdk_listener=-1;unlink(sdk_path);return NULL;
}
__attribute__((constructor))static void sdk_init(void){
 const char*enabled=getenv("HELPER_LINUX_SEND_BRIDGE");if(!enabled||strcmp(enabled,"1"))return;
 const char*pidstr=getenv("HELPER_LINUX_CONTROLLER_PID"),*path=getenv("HELPER_LINUX_SEND_SOCKET");char*end=NULL;
 if(!pidstr||!path||path[0]!='/'||strlen(path)>=sizeof(sdk_path))return;
 errno=0;long parent=strtol(pidstr,&end,10);if(errno||!end||*end||parent<=0||parent>INT32_MAX||getppid()!=(pid_t)parent)return;sdk_parent=(pid_t)parent;
#ifdef SDK_FIXTURE
 sdk_send=(sdk_entry)dlsym(RTLD_DEFAULT,"fixture_sdk_send");sdk_send_generic=(sdk_entry)dlsym(RTLD_DEFAULT,"fixture_sdk_send_generic");if(!sdk_send_generic)sdk_send_generic=sdk_send;sdk_str=(sdk_ctor)dlsym(RTLD_DEFAULT,"fixture_sdk_string");sdk_delete=(void(*)(void*))dlsym(RTLD_DEFAULT,"fixture_sdk_delete");
 if(!sdk_send||!sdk_str||!sdk_delete)return;
#else
 if(!check_hash())return;
 uintptr_t base=0;dl_iterate_phdr(main_build,&base);if(!base)return;
 static const unsigned char expected[]={0x41,0x57,0x41,0x56,0x41,0x54,0x53,0x48,0x81,0xec,0x98,0,0,0,0x48,0x89,0xcb};
 if(memcmp((void*)(base+0x6f19410),expected,sizeof(expected)))return;
 sdk_base=base;
 sdk_send=(sdk_entry)(base+0x6f19410);sdk_send_generic=(sdk_entry)(base+0x6f18b40);sdk_str=(sdk_ctor)(base+0x450e6a0);sdk_delete=(void(*)(void*))(base+0x450c140);
#endif
 char parentdir[108];strcpy(parentdir,path);char*slash=strrchr(parentdir,'/');if(!slash||slash==parentdir)return;*slash=0;
 struct stat st;if(lstat(parentdir,&st)||!S_ISDIR(st.st_mode)||st.st_uid!=geteuid()||(st.st_mode&0777)!=0700)return;
 sdk_listener=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);if(sdk_listener<0)return;
 struct sockaddr_un sa={.sun_family=AF_UNIX};strcpy(sa.sun_path,path);strcpy(sdk_path,path);
 if(bind(sdk_listener,(struct sockaddr*)&sa,sizeof(sa))||chmod(path,0600)||listen(sdk_listener,4)){close(sdk_listener);sdk_listener=-1;return;}
 sdk_download_hook_ready=sdk_install_image_context_capture();
 pthread_t worker;if(pthread_create(&worker,NULL,sdk_serve,NULL)){close(sdk_listener);sdk_listener=-1;unlink(path);return;}pthread_detach(worker);
}
